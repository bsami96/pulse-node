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

TARGET_TYPES = {"Komfort-Apartment"}

HEADERS = {"User-Agent": "Mozilla/5.0"}
STATE_PATH = "state.json"
TZ = ZoneInfo("Europe/Berlin")

POLL_INTERVAL_NO_FREE_SEC = 30
POLL_INTERVAL_FREE_SEC = 10

# ŞU AN SENİN TEST AYARIN
RUN_WINDOW_SEC = 38
MIN_REMAINING_TO_START_SCRAPE = 15

SEND_STILL_MESSAGES = True
MAX_STILL_PER_RUN = 3

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
    core = "\n".join([f"{t}|{n}|{l or ''}" for t, n, l in free_units_sorted])
    return sha1(core) if core else ""


def main():
    # 🔥 GENEL SÜRE ÖLÇÜMÜ
    script_t0 = time.monotonic()

    state = load_state()
    start = time.monotonic()
    deadline = start + RUN_WINDOW_SEC

    heartbeat_checked = False
    still_sent = 0
    loop_i = 0
    scrape_count = 0

    while True:
        loop_i += 1
        now = datetime.now(TZ)

        remaining = deadline - time.monotonic()
        if remaining < MIN_REMAINING_TO_START_SCRAPE:
            print(f"STOP_EARLY remaining={remaining:.1f}s")
            break

        # 🔥 SCRAPE SÜRE ÖLÇÜMÜ
        scrape_t0 = time.monotonic()
        try:
            total_komfort, free_units_sorted, status_counts, unknown_status = scrape_once()
        except Exception as e:
            print(f"SCRAPE_ERROR {type(e).__name__}: {e}")
            break

        scrape_dt = time.monotonic() - scrape_t0
        scrape_count += 1
        print(f"SCRAPE_SECONDS={scrape_dt:.1f} SCRAPE_COUNT={scrape_count}")
        print(
            f"TOTAL_KOMFORT={total_komfort} "
            f"FREI={status_counts['frei']} "
            f"RES={status_counts['reserviert']} "
            f"VER={status_counts['vermietet']} "
            f"UNKNOWN={unknown_status}"
        )

        # ---- Heartbeat: 10 ve 18 (DE saati) — ilk 5 dakikada 1 kez (run başına sadece 1 kez kontrol) ----
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

        if had_free and current_hash != last_hash:
            send_telegram(format_free_message("🚨 FREI!", now, free_units_sorted))
            state["last_free_hash"] = current_hash

        elif had_free and current_hash == last_hash:
            if SEND_STILL_MESSAGES and still_sent < MAX_STILL_PER_RUN:
                still_sent += 1
                send_telegram(format_free_message(f"🔔 STILL FREI [#{still_sent}]", now, free_units_sorted))

        elif (not had_free) and last_hash:
            send_telegram(f"❌ GONE ({now.strftime('%Y-%m-%d %H:%M:%S')} DE)")
            state["last_free_hash"] = ""

        interval = POLL_INTERVAL_FREE_SEC if had_free else POLL_INTERVAL_NO_FREE_SEC
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        sleep_for = min(interval, remaining)
        if sleep_for < 0.5:
            break

        print(f"SLEEP={sleep_for:.1f}s")
        time.sleep(sleep_for)

    save_state(state)

    # 🔥 TOPLAM SCRIPT SÜRESİ
    total_script = time.monotonic() - script_t0
    print(f"SCRIPT_SECONDS={total_script:.1f}")
    print("OK.")


if __name__ == "__main__":
    main()
