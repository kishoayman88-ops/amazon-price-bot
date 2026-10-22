"""
Amazon.eg price-drop scanner.

Every run it:
  1. Opens the Best Sellers and Movers & Shakers pages for each category.
  2. Reads the price of every product on those pages.
  3. Compares today's price with the item's usual price (median of past days).
  4. Sends a Telegram alert when the price drops by DROP_PERCENT or more.

Prices are kept in prices.json (saved back to GitHub after each run).
"""

import html
import json
import os
import random
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ---------------- Settings ----------------
BASE = "https://www.amazon.eg"
KINDS = ["bestsellers", "movers-and-shakers"]
DATA_FILE = "prices.json"

DROP_PERCENT = float(os.getenv("DROP_PERCENT", "70"))   # alert at this % drop or more
MAX_CATEGORIES = int(os.getenv("MAX_CATEGORIES", "40"))  # categories per page type
MIN_DAYS_HISTORY = 2      # days of prices needed before an item can alert
KEEP_DAYS = 30            # days of price history to keep per item
DELAY = (2.0, 4.0)        # seconds to wait between pages (be gentle)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_ERRORS = []   # saved into prices.json so problems are easy to see

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

PRICE_RE = re.compile(r"EGP\s*([\d,]+(?:\.\d{1,2})?)")
ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
CAT_RE = re.compile(r"/gp/(bestsellers|movers-and-shakers)/([a-z0-9][a-z0-9\-_]*)")


# ---------------- Telegram ----------------
def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        TG_ERRORS.append("missing secret: " + ("TELEGRAM_BOT_TOKEN " if not TOKEN else "")
                         + ("TELEGRAM_CHAT_ID" if not CHAT_ID else ""))
        print("[telegram]", TG_ERRORS[-1])
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        if r.status_code != 200:
            TG_ERRORS.append(f"{r.status_code}: {r.text[:200]}")
            print("[telegram] error:", TG_ERRORS[-1])
            return False
        return True
    except requests.RequestException as e:
        TG_ERRORS.append("network: " + str(e).replace(TOKEN, "***")[:200])
        print("[telegram] failed:", TG_ERRORS[-1])
        return False


# ---------------- Fetching ----------------
def is_blocked(page):
    low = page.lower()
    return ("validatecaptcha" in low
            or "type the characters you see" in low
            or "sorry, we just need to make sure you're not a robot" in low)


def fetch(session, path):
    url = path if path.startswith("http") else BASE + path
    for attempt in range(3):
        try:
            r = session.get(url, headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }, timeout=30)
            if r.status_code == 200 and not is_blocked(r.text):
                return r.text
            print(f"  blocked/err {r.status_code} on {url} (try {attempt + 1})")
        except requests.RequestException as e:
            print(f"  network error on {url}: {e}")
        time.sleep(8 * (attempt + 1))
    return None


def category_paths(page, kind):
    found = []
    for k, slug in CAT_RE.findall(page):
        if k != kind or slug.startswith("ref"):
            continue
        path = f"/-/en/gp/{kind}/{slug}/"
        if path not in found:
            found.append(path)
    return found


# ---------------- Parsing ----------------
def parse_price(text):
    if re.search(r"EGP[\d\s,.]+-\s*EGP", text):   # price range -> skip
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_items(page):
    """Return {asin: {"title", "price"}} from a best-seller style page."""
    soup = BeautifulSoup(page, "html.parser")
    cards = soup.select('[id="gridItemRoot"]') or soup.select("div[data-asin]")
    items = {}
    for card in cards:
        asin = card.get("data-asin")
        if not asin:
            inner = card.select_one("[data-asin]")
            asin = inner.get("data-asin") if inner else None
        if not asin:
            link = card.find("a", href=ASIN_RE)
            m = ASIN_RE.search(link["href"]) if link else None
            asin = m.group(1) if m else None
        if not asin or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        price = parse_price(card.get_text(" ", strip=True))
        if price is None:
            continue
        img = card.find("img", alt=True)
        title = (img["alt"].strip() if img and img["alt"].strip()
                 else card.get_text(" ", strip=True)[:120])
        items[asin] = {"title": title[:200], "price": price}
    return items


# ---------------- Price logic ----------------
def load_db():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}
    db.setdefault("items", {})
    db.setdefault("meta", {})
    return db


def save_db(db):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))


def check_item(db, asin, item, today):
    """Update history; return alert dict or None."""
    rec = db["items"].setdefault(asin, {"t": item["title"], "h": {}, "alerted": None})
    rec["t"] = item["title"]
    rec["seen"] = today
    price = item["price"]

    past = [p for d, p in rec["h"].items() if d != today]
    alert = None
    if len(past) >= MIN_DAYS_HISTORY:
        usual = statistics.median(past)
        limit = usual * (1 - DROP_PERCENT / 100)
        if price <= limit:
            last = rec.get("alerted")
            if last is None or price < last:      # alert again only if even lower
                rec["alerted"] = price
                alert = {"asin": asin, "title": item["title"],
                         "price": price, "usual": usual}
        else:
            rec["alerted"] = None                  # price recovered -> reset

    rec["h"][today] = price                        # one price per day (latest)
    if len(rec["h"]) > KEEP_DAYS:
        for d in sorted(rec["h"])[:-KEEP_DAYS]:
            del rec["h"][d]
    return alert


def forget_old(db, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    old = [a for a, r in db["items"].items() if r.get("seen", today) < cutoff]
    for a in old:
        del db["items"][a]
    return len(old)


def alert_text(a):
    drop = round((1 - a["price"] / a["usual"]) * 100)
    return (f"🔥 <b>Price drop {drop}%</b>\n"
            f"{html.escape(a['title'])}\n\n"
            f"Now: <b>EGP {a['price']:,.2f}</b>\n"
            f"Usual: EGP {a['usual']:,.2f}\n"
            f"https://www.amazon.eg/dp/{a['asin']}")


# ---------------- Main ----------------
def main():
    test_ok = True
    if os.getenv("TEST_ALERT", "").lower() == "true":
        test_ok = send_telegram("✅ Amazon.eg price bot is working. "
                      f"It will alert on drops of {DROP_PERCENT:.0f}% or more.")

    db = load_db()
    today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()  # Cairo
    session = requests.Session()

    pages_ok = pages_failed = 0
    seen = {}
    for kind in KINDS:
        root = fetch(session, f"/-/en/gp/{kind}/")
        if root is None:
            pages_failed += 1
            continue
        pages_ok += 1
        seen.update(parse_items(root))
        cats = category_paths(root, kind)[:MAX_CATEGORIES]
        print(f"{kind}: {len(cats)} categories")
        for path in cats:
            time.sleep(random.uniform(*DELAY))
            page = fetch(session, path)
            if page is None:
                pages_failed += 1
                continue
            pages_ok += 1
            seen.update(parse_items(page))

    alerts = [a for asin, item in seen.items()
              if (a := check_item(db, asin, item, today))]
    removed = forget_old(db, today)

    for a in sorted(alerts, key=lambda x: x["price"] / x["usual"]):
        send_telegram(alert_text(a))
        time.sleep(1)

    # Tell the user (max once a day) if Amazon blocked most pages.
    total = pages_ok + pages_failed
    if total and pages_failed / total > 0.5 and db["meta"].get("block_notice") != today:
        db["meta"]["block_notice"] = today
        send_telegram(f"⚠️ Amazon blocked {pages_failed} of {total} pages this run. "
                      "The bot will keep trying.")

    db["meta"]["telegram_errors"] = TG_ERRORS[-5:]
    db["meta"]["token_shape_ok"] = bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TOKEN))
    db["meta"]["chat_id_shape_ok"] = bool(re.fullmatch(r"-?\d+", CHAT_ID))
    db["meta"]["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_db(db)
    print(f"pages ok={pages_ok} failed={pages_failed} items={len(seen)} "
          f"tracked={len(db['items'])} alerts={len(alerts)} forgotten={removed}")
    return 0 if test_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
