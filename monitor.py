#!/usr/bin/env python3
"""
BAGESTRA Wohnungsmonitor
========================
Prüft die BAGESTRA-Webseite auf neue Wohnungsinserate und
benachrichtigt via Telegram bei Änderungen.

Läuft als GitHub Actions Scheduled Workflow (alle 5 Minuten).
Zugangsdaten werden über Umgebungsvariablen / GitHub Secrets geladen.
"""

import os
import sys
import json
import hashlib
import re
import requests
from datetime import datetime, timezone

# ============================================================
# Konfiguration aus Umgebungsvariablen
# ============================================================

URL = "https://bagestra.ch/objekte/index.php?cat=whg"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = "state.json"


def fetch_page() -> str:
    """Holt den HTML-Inhalt der BAGESTRA-Seite."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-CH,de;q=0.9",
    }
    resp = requests.get(URL, headers=headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def extract_content(html: str) -> dict:
    """
    Extrahiert den relevanten Seiteninhalt.
    Gibt ein Dict zurück mit: text, listings, hat_inserate
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Navigation, Footer, Scripts entfernen
    for tag in soup.find_all(["nav", "footer", "script", "style", "header"]):
        tag.decompose()

    body = soup.body
    if not body:
        return {"text": "", "listings": [], "hat_inserate": False}

    content_text = body.get_text(separator="\n", strip=True)

    # --- Inserate erkennen ---
    listings = []

    # 1) Links zu Detailseiten
    soup2 = BeautifulSoup(html, "html.parser")
    for link in soup2.find_all("a", href=True):
        href = link.get("href", "")
        text = link.get_text(strip=True)
        keywords = ["objekt", "detail", "inserat", "wohnung", "besichtigung"]
        skip = ["index.php?cat=", "homegate", "mieterinformationen", "kontakt",
                "baugenossenschaft", "liegenschaften", "impressum", "datenschutz",
                "javascript:", "#", "mailto:"]
        if any(kw in href.lower() for kw in keywords):
            if not any(s in href.lower() for s in skip) and text:
                full_url = href if href.startswith("http") else f"https://bagestra.ch/objekte/{href}"
                listings.append({"title": text, "url": full_url})

    # 2) Typische Inserat-Muster im Text
    patterns = [
        r'(\d[\d.,]*[\s-]*Zimmer[\w\s-]*(?:Wohnung)?)',
        r'((?:Netto)?[Mm]ietzins[:\s]*(?:CHF|Fr\.?)\s*[\d\'.]+)',
        r'(Bezugstermin[:\s]*[\d.]+\s*\w*)',
    ]
    for pat in patterns:
        for match in re.finditer(pat, content_text):
            listings.append({"title": match.group(1).strip(), "url": URL})

    # Duplikate entfernen
    seen = set()
    unique_listings = []
    for l in listings:
        key = l["title"]
        if key not in seen:
            seen.add(key)
            unique_listings.append(l)

    hat_inserate = len(unique_listings) > 0

    return {
        "text": content_text,
        "listings": unique_listings,
        "hat_inserate": hat_inserate,
    }


def compute_hash(text: str) -> str:
    """SHA-256 Hash des Seiteninhalts."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> dict:
    """Letzten Zustand aus state.json laden."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"hash": "", "last_check": "", "had_listings": False}


def save_state(content_hash: str, had_listings: bool):
    """Aktuellen Zustand in state.json speichern."""
    state = {
        "hash": content_hash,
        "last_check": datetime.now(timezone.utc).isoformat(),
        "had_listings": had_listings,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(message: str):
    """Nachricht via Telegram Bot senden."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen)")
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
        print("✅ Telegram-Nachricht gesendet!")
        return True
    else:
        print(f"❌ Telegram-Fehler: {resp.status_code} – {resp.text}")
        return False


def run():
    """Hauptlogik: Seite prüfen, vergleichen, benachrichtigen."""
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    print(f"[{now}] Prüfe BAGESTRA-Seite...")

    # 1) Seite abrufen
    try:
        html = fetch_page()
    except Exception as e:
        print(f"❌ Fehler beim Abrufen: {e}")
        sys.exit(1)

    # 2) Inhalt extrahieren
    data = extract_content(html)
    content_hash = compute_hash(data["text"])

    # 3) Mit letztem Zustand vergleichen
    state = load_state()

    if not state["hash"]:
        # Erster Lauf
        save_state(content_hash, data["hat_inserate"])
        print(f"📋 Erster Lauf – Zustand gespeichert.")
        if data["hat_inserate"]:
            print(f"   {len(data['listings'])} Inserat(e) bereits vorhanden.")
        else:
            print("   Keine Inserate vorhanden.")
        return

    if content_hash == state["hash"]:
        print("😴 Keine Änderung.")
        return

    # --- Änderung erkannt! ---
    print("🔔 ÄNDERUNG ERKANNT!")

    if data["hat_inserate"]:
        listings_text = "\n".join(
            f"  • {l['title']}" for l in data["listings"]
        )
        if not state.get("had_listings", False):
            # Vorher keine Inserate → jetzt schon!
            msg = (
                "🏠🔥 <b>NEUE Wohnungen auf BAGESTRA!</b>\n\n"
                f"{listings_text}\n\n"
                f'👉 <a href="{URL}">Jetzt ansehen & anmelden!</a>\n\n'
                f"⏰ {now}"
            )
        else:
            # Inserate haben sich geändert
            msg = (
                "🏠 <b>BAGESTRA Inserate geändert!</b>\n\n"
                f"{listings_text}\n\n"
                f'👉 <a href="{URL}">Jetzt prüfen</a>\n\n'
                f"⏰ {now}"
            )
    else:
        msg = (
            "🏠 <b>BAGESTRA Seite aktualisiert</b>\n\n"
            "Die Seite hat sich geändert – möglicherweise neue Inhalte.\n\n"
            f'👉 <a href="{URL}">Jetzt prüfen</a>\n\n'
            f"⏰ {now}"
        )

    send_telegram(msg)
    save_state(content_hash, data["hat_inserate"])


if __name__ == "__main__":
    run()
