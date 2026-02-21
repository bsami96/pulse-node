import os
import re
import json
import html as ihtml
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = os.environ["URL"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# SADECE bunu takip ediyoruz
TARGET_TYPES = {"Komfort-Apartment"}

HEADERS = {"User-Agent": "Mozilla/5.0"}
STATE_PATH = "state.json"
TZ = ZoneInfo("Europe/Berlin")  # Almanya saati


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # last_free_hash artık kullanılmayacak ama state yapısı bozulmasın
        return {"last_free_hash": "", "last_heartbeat_key": ""}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def base_type(title: str) -> str:
    return re.sub(r"\s*Nr\..*$", "", title).strip()


def extract_status_and_link(data_text: str):
    decoded = ihtml.unescape(data_text)

    status = None
    m = re.search(r"Status:\s*(?:<[^>]+>)*\s*([A-Za-zÄÖÜäöüß]+)", decoded)
    if m:
        status = m.group(1).strip().lower()

    link = None
    lm = re.search(r'href=(?:"|&quot;)([^"&]+)(?:"|&quot;)', decoded, flags=re.I)
    if lm:
        link = lm.group(1)

    return status, link


def send_telegram(text: str):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    ).raise_for_status()


def main():
    state = load_state()

    # 1) Sayfayı çek
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    anchors = soup.select("a.apartment")

    # 2) Sadece Komfort-Apartment tara
    seen = set()
    free_units = []

    for a in anchors:
        title = a.get("data-original-title") or a.get("title") or ""
        typ = base_type(title)
        if typ not in TARGET_TYPES:
            continue

        number = a.get_text(" ", strip=True).strip()
        key = (typ, number)
        if key in seen:
            continue
        seen.add(key)

        data_text = a.get("data-text") or ""
        status, link = extract_status_and_link(data_text)

        if status is None and "unit_free" in data_text:
            status = "frei"

        if status == "frei":
            free_units.append((typ, number, link))

    now = datetime.now(TZ)

    # 3) Heartbeat: 10:00 ve 18:00 (Almanya saati)
    if now.hour in (10, 18) and now.minute == 0:
        hb_key = now.strftime("%Y-%m-%d_%H")
        if state.get("last_heartbeat_key") != hb_key:
            send_telegram(
                f"🫀 BOT CANLI ({now.strftime('%Y-%m-%d %H:%M')} DE)\nKomfort-Apartment takip aktif."
            )
            state["last_heartbeat_key"] = hb_key

    # 4) SPAM MODU: Frei varsa HER 5 DK'DA BİR mesaj at
    free_units_sorted = sorted(free_units, key=lambda x: (x[0], x[1]))

    if free_units_sorted:
        lines = [f"🚨 FREI! ({now.strftime('%Y-%m-%d %H:%M')} DE)"]
        for typ, number, link in free_units_sorted:
            lines.append(f"- {typ} | {number}")
            if link:
                lines.append(f"  {link}")
        send_telegram("\n".join(lines))

    # last_free_hash artık önemli değil; ama dosyayı stabil tutalım
    state["last_free_hash"] = ""

    save_state(state)
    print("OK. free_units:", len(free_units_sorted))


if __name__ == "__main__":
    main()
