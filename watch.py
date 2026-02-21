import os
import re
import html as ihtml
import requests
from bs4 import BeautifulSoup

URL = os.environ["URL"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TARGET_TYPES = {"Komfort-Apartment", "Komfort L-Apartment"}

HEADERS = {"User-Agent": "Mozilla/5.0"}

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
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")
    anchors = soup.select("a.apartment")

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

    if free_units:
        lines = ["✅ MÜSAİT VAR!"]
        for typ, number, link in free_units:
            lines.append(f"- {typ} | {number}")
            if link:
                lines.append(f"  {link}")
        send_telegram("\n".join(lines))
    else:
        print("No availability.")

if __name__ == "__main__":
    main()
