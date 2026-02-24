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

# ÖNEMLİ: cron 1 dk -> run'ı net kısa tut
RUN_WINDOW_SEC = 45              # 60 altı garantiye yakın
MIN_REMAINING_TO_START_SCRAPE = 8  # kalan süre azsa yeni scrape'e girme

# STILL spam kontrol
SEND_STILL_MESSAGES = True
MAX_STILL_PER_RUN = 3

# Timeout'ları kısalt (run uzamasın)
HTTP_GET_TIMEOUT = 20
TELEGRAM_TIMEOUT = 10


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
        timeout=TELEGRAM_TIMEOUT,
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
    r = requests.get(URL, headers=HEADERS, timeout=HTTP_GET_TIMEOUT)
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

        if status is None and "unit_free" in data_text:
            status = "frei"

        if status in status_counts:
            status_counts[status] += 1
        elif status is not None:
            unknown_status += 1

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


def free_hash(free_units_sorted) -> str:
    # aynı listeyi stabil hash'le
    core = "\n".join([f"{t}|{n}|{l or ''}" for t, n, l in free_units_sorted])
    return sha1(core) if core else ""


def main():
    state = load_state()
    start = time.monotonic()
    deadline = start + RUN_WINDOW_SEC

    # Heartbeat sadece ilk turda
    heartbeat_checked = False

    still_sent = 0
    loop_i = 0

    while True:
        loop_i += 1
        now = datetime.now(TZ)

        remaining = deadline - time.monotonic()
        if remaining < MIN_REMAINING_TO_START_SCRAPE:
            break

        # ---- Scrape ----
        try:
            total_komfort, free_units_sorted, status_counts, unknown_status = scrape_once()
        except requests.HTTPError as e:
            # site kızdıysa bu run'ı uzatma
            try:
                send_telegram(f"⚠️ HTTPError: {e} ({now.strftime('%H:%M:%S')} DE)")
            except Exception:
                pass
            break
        except Exception as e:
            try:
                send_telegram(f"⚠️ ERROR: {type(e).__name__}: {e}")
            except Exception:
                pass
            break

        # ---- Heartbeat (günde 2 kez) sadece ilk döngüde ----
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
        current_hash = free_hash(free_units_sorted)
        last_hash = state.get("last_free_hash", "")

        # ---- Mesaj mantığı (run'lar arası düzgün) ----
        if had_free and current_hash != last_hash:
            # yeni free yakaladık (veya free listesi değişti)
            send_telegram(format_free_message("🚨 FREI!", now, free_units_sorted))
            state["last_free_hash"] = current_hash

        elif had_free and current_hash == last_hash:
            # free devam ediyor -> still (kısıtlı)
            if SEND_STILL_MESSAGES and still_sent < MAX_STILL_PER_RUN:
                still_sent += 1
                send_telegram(format_free_message(f"🔔 STILL FREI [#{still_sent}]", now, free_units_sorted))

        elif (not had_free) and last_hash:
            # daha önce free vardı, şimdi yok
            send_telegram(f"❌ GONE ({now.strftime('%Y-%m-%d %H:%M:%S')} DE) — artık frei değil.")
            state["last_free_hash"] = ""

        # ---- Bir sonraki scrape'e kadar bekle (kalan süreye saygı) ----
        interval = POLL_INTERVAL_FREE_SEC if had_free else POLL_INTERVAL_NO_FREE_SEC
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        sleep_for = min(interval, remaining)
        if sleep_for < 0.5:
            break
        time.sleep(sleep_for)

    save_state(state)
    print("OK.")


if __name__ == "__main__":
    main()
