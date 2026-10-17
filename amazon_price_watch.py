#!/usr/bin/env python3
"""
Amazon India price watcher -> Telegram alert every 30 minutes.
Watches multiple products; add more URLs to PRODUCTS below.

Setup:
    pip install requests beautifulsoup4 lxml python-dotenv
    # optional fallback if Amazon blocks plain requests:
    pip install playwright && playwright install chromium

Env vars (set in the shell, or in a .env file next to this script):
    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     your chat id (message @userinfobot, or use getUpdates)
    USE_PLAYWRIGHT=1     (optional) force headless-browser fetching

Run:
    python amazon_price_watch.py
"""

import os
import random
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:  # optional: load TELEGRAM_* from a .env file next to this script
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def load_env_file(path: str = ".env") -> None:
    """Minimal .env loader (no extra dependency). Real env vars take priority."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (path, os.path.join(here, ".env")):
        if os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


load_env_file()

# ---- products to watch: add more URLs here ----
PRODUCTS = [
    "https://www.amazon.in/abc/dp/B0GVRZD89W?th=1",
    "https://www.amazon.in/dp/B0GTZKGNM6?th=1",
]

INTERVAL_SECONDS = 30 * 60
DELAY_BETWEEN_PRODUCTS = 5  # seconds, to avoid hammering Amazon
ALERT_AFTER_FAILURES = 3  # send an "I'm failing" alert after N consecutive failures

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT") == "1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------- fetching ----------
def fetch_html_requests(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_html_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"], locale="en-IN"
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()
        return html


def looks_blocked(html: str) -> bool:
    low = html.lower()
    return (
        "captcha" in low
        or "robot check" in low
        or "api-services-support@amazon.com" in low
    )


def fetch_html(url: str) -> str:
    if USE_PLAYWRIGHT:
        return fetch_html_playwright(url)
    try:
        html = fetch_html_requests(url)
        if not looks_blocked(html):
            return html
    except requests.RequestException:
        pass
    # fall back to a real browser if available
    return fetch_html_playwright(url)


# ---------- parsing ----------
def to_number(text: str):
    """Extract the first number from text like 'M.R.P.: ₹3,06,630.00' or '1,21,990'."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Amazon product"

    price = mrp = None

    # 1) logged-out layout: visible price inside the priceToPay block
    #    (the span.a-offscreen in this block is empty, so use a-price-whole)
    el = soup.select_one(
        "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-price-whole, "
        "span.apex-pricetopay-value span.a-price-whole, "
        "span.priceToPay span.a-price-whole"
    )
    if el:
        price = to_number(el.get_text())

    # 2) accessibility label, e.g. "₹1,21,990.00 with 60 percent savings"
    if price is None:
        el = soup.select_one("#apex-pricetopay-accessibility-label")
        if el:
            m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", el.get_text())
            if m:
                price = to_number(m.group(1))

    # 3) logged-in layout: data attributes on the price block
    block = soup.select_one("[data-csa-c-price-to-pay]")
    if block:
        if price is None:
            price = to_number(block.get("data-csa-c-price-to-pay"))
        mrp = to_number(block.get("data-csa-c-strike-through-price"))

    # MRP (strike-through price) from visible text
    if mrp is None:
        el = soup.select_one(
            "span.apex-basisprice-value span.a-offscreen, "
            "span.apex-basisprice-offscreen-label"
        )
        if el:
            mrp = to_number(el.get_text())

    if price is None:
        raise ValueError("Could not find price on page (layout change or block page)")

    return {"title": title, "price": price, "mrp": mrp}


# ---------- telegram ----------
def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[WARN] Telegram env vars missing; message not sent:\n" + text)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"[WARN] Telegram error {resp.status_code}: {resp.text}")


def inr(n) -> str:
    # Indian digit grouping: 1,21,990
    s = f"{int(round(n))}"
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"₹{head},{tail}"


def build_message(url: str, info: dict, last_price) -> str:
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    lines = [f"🛒 {info['title'][:120]}", f"💰 Current price: {inr(info['price'])}"]
    if info["mrp"]:
        off = round((1 - info["price"] / info["mrp"]) * 100)
        lines.append(f"🏷 MRP: {inr(info['mrp'])} ({off}% off)")
    if last_price is not None:
        diff = info["price"] - last_price
        if diff < 0:
            lines.append(f"📉 Dropped {inr(abs(diff))} since last check")
        elif diff > 0:
            lines.append(f"📈 Up {inr(diff)} since last check")
        else:
            lines.append("➖ No change since last check")
    lines.append(f"🕒 {now}")
    lines.append(url)
    return "\n".join(lines)


# ---------- main loop ----------
def main():
    last_price = {url: None for url in PRODUCTS}
    failures = {url: 0 for url in PRODUCTS}

    while True:
        for i, url in enumerate(PRODUCTS):
            if i:
                time.sleep(DELAY_BETWEEN_PRODUCTS)
            try:
                info = parse_page(fetch_html(url))
                send_telegram(build_message(url, info, last_price[url]))
                print(f"[{datetime.now():%H:%M:%S}] {url} price {info['price']}")
                last_price[url] = info["price"]
                failures[url] = 0
            except Exception as e:
                failures[url] += 1
                print(f"[{datetime.now():%H:%M:%S}] {url} error ({failures[url]}): {e}")
                if failures[url] == ALERT_AFTER_FAILURES:
                    send_telegram(
                        f"⚠️ Price watcher failed {failures[url]} times in a row "
                        f"for {url}: {e}"
                    )
        # small jitter so requests aren't perfectly periodic
        time.sleep(INTERVAL_SECONDS + random.randint(-60, 60))


if __name__ == "__main__":
    main()