#!/usr/bin/env python3
"""
Genossenschafts-Wohnungsmonitor (Multi-Site)
=============================================
Überwacht mehrere Zürcher Genossenschafts-Webseiten auf neue
Wohnungsinserate und benachrichtigt via Telegram bei Änderungen.

Jede URL wird separat überwacht mit eigenem State in state.json.
Läuft als GitHub Actions Scheduled Workflow (alle 5 Minuten).
"""

import os
import sys
import json
import hashlib
import re
import requests
from datetime import datetime, timezone

# ============================================================
# Konfiguration
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = "state.json"

# Überwachte Seiten: key = kurzer Name, url = Seiten-URL
SITES = {
    "BAGESTRA": {
        "url": "https://bagestra.ch/objekte/index.php?cat=whg",
        "emoji": "🏠",
    },
    "BAHOGE": {
        "url": "https://bahoge.ch/Vermietung/",
        "emoji": "🏡",
    },
    "Brunnenrain": {
        "url": "https://brunnenrain.ch/vermietung/index.html",
        "emoji": "🏘️",
    },
    "WSGZ": {
        "url": "https://wsgz.ch/de/Inserate",
        "emoji": "🏢",
    },
}

# ============================================================
# Hilfsfunktionen
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9",
}


def fetch_page(url: str) -> str:
    """Holt den HTML-Inhalt einer Seite."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def extract_content(html: str) -> dict:
    """
    Extrahiert den relevanten Seiteninhalt.
    Gibt zurück: {text, listings, hat_inserate}
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Rauschen entfernen
    for tag in soup.find_all(["nav", "footer", "script", "style", "header", "noscript"]):
        tag.decompose()

    body = soup.body
    if not body:
        return {"text": "", "listings": [], "hat_inserate": False}

    content_text = body.get_text(separator="\n", strip=True)

    # --- Inserate erkennen ---
    listings = []

    # 1) Links zu Detailseiten
    soup2 = BeautifulSoup(html, "html.parser")
    positive_kw = ["objekt", "detail", "inserat", "wohnung", "besichtigung", "zimmer"]
    skip_kw = [
        "index.php?cat=", "homegate.ch", "mieterinformationen", "kontakt",
        "baugenossenschaft", "liegenschaften", "impressum", "datenschutz",
        "javascript:", "#", "mailto:", "siedlung", "ueber-uns", "uber-uns",
        "service", "reparatur", "reglemente", "formulare", "gemeinschaft",
        "geschichte", "vorstand", "agenda", "depositenkasse", "sozialberatung",
        "publikationen", "rund-ums-mieten", "wohnen-im-alter", "news",
        "startseite", "zurück", "back",
    ]
    for link in soup2.find_all("a", href=True):
        href = link.get("href", "")
        text = link.get_text(strip=True)
        if any(kw in href.lower() for kw in positive_kw):
            if not any(s in href.lower() for s in skip_kw) and text and len(text) > 3:
                full_url = href if href.startswith("http") else href
                listings.append({"title": text, "url": full_url})

    # 2) Typische Inserat-Muster im Text
    patterns = [
        r'(\d[\d.,]*[\s-]*Zimmer[\w\s-]*(?:Wohnung|WHG)?)',
        r'((?:Netto)?[Mm]ietzins[:\s]*(?:CHF|Fr\.?)\s*[\d\'.]+)',
        r'(Bezugstermin[:\s]*[\d.\s]*\w+\s*\d{4})',
    ]
    for pat in patterns:
        for match in re.finditer(pat, content_text, re.IGNORECASE):
            listings.append({"title": match.group(1).strip(), "url": ""})

    # Duplikate entfernen
    seen = set()
    unique = []
    for l in listings:
        key = l["title"]
        if key not in seen:
            seen.add(key)
            unique.append(l)

    # "Keine Inserate"-Marker
    leer_marker = [
        "zurzeit sind keine wohnungen",
        "sind keine angebote aufgeschaltet",
        "aktuell keine mietobjekte",
        "keine freien wohnungen",
        "derzeit keine wohnungen",
        "momentan keine wohnungen",
        "aktuell sind keine",
        "keine inserate vorhanden",
    ]
    seite_sagt_leer = any(m in content_text.lower() for m in leer_marker)

    hat_inserate = len(unique) > 0
    if seite_sagt_leer and len(unique) == 0:
        hat_inserate = False

    return {
        "text": content_text,
        "listings": unique,
        "hat_inserate": hat_inserate,
    }


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> dict:
    """Lädt den gesamten State (alle Sites)."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Speichert den gesamten State."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram(message: str) -> bool:
    """Nachricht via Telegram Bot senden."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠️  Telegram nicht konfiguriert")
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    resp = requests.post(api_url, json=payload, timeout=15)
    if resp.status_code == 200:
        print("  ✅ Telegram gesendet!")
        return True
    else:
        print(f"  ❌ Telegram-Fehler: {resp.status_code} – {resp.text}")
        return False


# ============================================================
# Hauptlogik
# ============================================================

def check_site(name: str, site: dict, state: dict) -> dict:
    """
    Prüft eine einzelne Site.
    Gibt den aktualisierten State-Eintrag zurück.
    """
    url = site["url"]
    emoji = site["emoji"]
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    print(f"\n{'─' * 50}")
    print(f"  {emoji} {name}: {url}")
    print(f"{'─' * 50}")

    site_state = state.get(name, {"hash": "", "had_listings": False})

    # 1) Seite abrufen
    try:
        html = fetch_page(url)
    except Exception as e:
        print(f"  ❌ Fehler beim Abrufen: {e}")
        return site_state

    # 2) Inhalt analysieren
    data = extract_content(html)
    content_hash = compute_hash(data["text"])

    # 3) Erster Lauf?
    if not site_state.get("hash"):
        print(f"  📋 Erster Lauf – Zustand gespeichert.")
        if data["listings"]:
            print(f"     {len(data['listings'])} Inserat(e) vorhanden")
        else:
            print(f"     Keine Inserate erkennbar")
        return {
            "hash": content_hash,
            "last_check": now,
            "had_listings": data["hat_inserate"],
        }

    # 4) Vergleichen
    if content_hash == site_state["hash"]:
        print(f"  😴 Keine Änderung")
        site_state["last_check"] = now
        return site_state

    # --- Änderung erkannt! ---
    print(f"  🔔 ÄNDERUNG bei {name}!")

    listings_text = ""
    if data["listings"]:
        listings_text = "\n".join(f"  • {l['title']}" for l in data["listings"])

    had_before = site_state.get("had_listings", False)

    if data["hat_inserate"] and not had_before:
        msg = (
            f"{emoji}🔥 <b>NEUE Wohnungen bei {name}!</b>\n\n"
            + (f"{listings_text}\n\n" if listings_text else "")
            + f'👉 <a href="{url}">Jetzt ansehen &amp; anmelden!</a>\n'
            + f"⏰ {now}"
        )
    elif data["hat_inserate"]:
        msg = (
            f"{emoji} <b>Änderung bei {name}!</b>\n\n"
            + (f"{listings_text}\n\n" if listings_text else "Die Inserate haben sich geändert.\n\n")
            + f'👉 <a href="{url}">Jetzt prüfen</a>\n'
            + f"⏰ {now}"
        )
    else:
        msg = (
            f"{emoji} <b>{name}: Seite aktualisiert</b>\n\n"
            f"Inhalt hat sich geändert – könnte neue Info sein.\n\n"
            f'👉 <a href="{url}">Prüfen</a>\n'
            f"⏰ {now}"
        )

    send_telegram(msg)

    return {
        "hash": content_hash,
        "last_check": now,
        "had_listings": data["hat_inserate"],
    }


def run():
    """Alle Sites prüfen."""
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    print(f"{'═' * 50}")
    print(f"  🏠 Wohnungsmonitor – {now}")
    print(f"  {len(SITES)} Seiten zu prüfen")
    print(f"{'═' * 50}")

    state = load_state()

    for name, site in SITES.items():
        state[name] = check_site(name, site, state)

    save_state(state)
    print(f"\n💾 State gespeichert")
    print(f"✅ Fertig.\n")


if __name__ == "__main__":
    run()
