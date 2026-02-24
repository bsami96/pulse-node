import os
import re
import json
import html as ihtml
import hashlib
import time
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

# === 2 vites polling ===
POLL_INTERVAL_NO_FREE_SEC = 30   # frei yokken
POLL_INTERVAL_FREE_SEC = 10      # frei varken
RUN_WINDOW_SEC = 55              # cron 1 dk: run'ı 55 sn civarı tut

# STILL mesajları çok spam olabilir; True ise her free-check'te atar
SEND_STILL_MESSAGES = True


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
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


def scrape_once():
    """
    Sayfayı çekip parse eder.
    Return:
      total_komfort (int)
      free_units_sorted (list[(typ, number, link)])
      status_counts (dict)
      unknown_status (int)
    """
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    anchors = soup.select("a.apartment")

    seen = set()
    free_units = []

    status_counts = {"frei": 0, "reserviert": 0, "vermietet": 0}
    unknown_status = 0

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

        # bazen status regex kaçırırsa
        if status is None and "unit_free" in data_text:
            status = "frei"

        # sayaçlar (sadece bildiklerimizi say)
        if status in status_counts:
            status_counts[status] += 1
        elif status is not None:
            unknown_status += 1

        # frei listesi
        if status == "frei":
            free_units.append((typ, number, link))

    total_komfort = len(seen)
    free_units_sorted = sorted(free_units, key=lambda x: (x[0], x[1]))
    return total_komfort, free_units_sorted, status_counts, unknown_status


def format_free_message(prefix: str, now: datetime, free_units_sorted):
    lines = [f"{prefix} ({now.strftime('%Y-%m-%d %H:%M:%S')} DE)"]
    for typ, number, link in free_units_sorted:
        lines.append(f"- {typ} | {number}")
        if link:
            lines.append(f"  {link}")
    return "\n".join(lines)


def main():
    state = load_state()
    start = time.monotonic()

    # Heartbeat'i sadece ilk turda değerlendireceğiz
    heartbeat_checked = False

    # Run boyunca önceki tur "frei var mıydı?" takip etmek için
    prev_had_free = False

    loop_i = 0
    while True:
        loop_i += 1
        now = datetime.now(TZ)

        # ---- Scrape ----
        try:
            total_komfort, free_units_sorted, status_counts, unknown_status = scrape_once()
        except requests.HTTPError as e:
            # 429/403 gibi durumlarda site kızmış olabilir; bu run'ı bitir
            send_telegram(f"⚠️ HTTPError: {e} ({now.strftime('%H:%M:%S')} DE)")
            break
        except Exception as e:
            send_telegram(f"⚠️ ERROR: {type(e).__name__}: {e}")
            break

        # ---- Heartbeat (günde 2 kez) sadece ilk döngüde çalışsın ----
        if not heartbeat_checked:
            heartbeat_checked = True
            if now.hour in (10, 18) and now.minute < 5:
                hb_key = now.strftime("%Y-%m-%d_%H")
                if state.get("last_heartbeat_key") != hb_key:
                    msg = (
                        f"🫀 Günlük durum ({now.strftime('%Y-%m-%d %H:%M')} DE)\n"
                        f"Bot aktif\n"
                        f"Komfort anchor: {total_komfort}\n"
                        f"Status frei: {status_counts['frei']}\n"
                        f"Status reserviert: {status_counts['reserviert']}\n"
                        f"Status vermietet: {status_counts['vermietet']}\n"
                        f"Unknown status: {unknown_status}"
                    )
                    send_telegram(msg)
                    state["last_heartbeat_key"] = hb_key

        had_free = bool(free_units_sorted)

        # ---- Mesaj mantığı ----
        if had_free and not prev_had_free:
            # ilk kez free gördük -> büyük alarm
            send_telegram(format_free_message("🚨 FREI!", now, free_units_sorted))

        elif had_free and prev_had_free:
            # free devam ediyor -> still mesajı (istersen)
            if SEND_STILL_MESSAGES:
                send_telegram(format_free_message(f"🔔 STILL FREI [#{loop_i}]", now, free_units_sorted))

        elif (not had_free) and prev_had_free:
            # free varken gitti
            send_telegram(f"❌ GONE ({now.strftime('%Y-%m-%d %H:%M:%S')} DE) — artık frei değil.")

        prev_had_free = had_free

        # ---- Run penceresi kontrolü ----
        elapsed = time.monotonic() - start
        if elapsed >= RUN_WINDOW_SEC:
            break

        # ---- Bir sonraki scrape'e kadar bekle (2 vites) ----
        interval = POLL_INTERVAL_FREE_SEC if had_free else POLL_INTERVAL_NO_FREE_SEC

        # kalan süreyi aşma (run window'a saygı)
        remaining = RUN_WINDOW_SEC - elapsed
        sleep_for = min(interval, max(0, remaining))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)

    # state dosyasını stabil tutalım
    state["last_free_hash"] = ""
    save_state(state)
    print("OK.")


if __name__ == "__main__":
    main()
