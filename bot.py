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


# ---------------- Loop mode ----------------
FAST_KINDS = ["movers-and-shakers"]   # checked every FAST_EVERY minutes
SLOW_KINDS = ["bestsellers"]          # checked every SLOW_EVERY minutes
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0"))   # 0 = one full pass, then stop
FAST_EVERY = float(os.getenv("FAST_EVERY", "3"))
SLOW_EVERY = float(os.getenv("SLOW_EVERY", "30"))
SAVE_EVERY = 30                       # minutes between saves to GitHub


def cairo_today():
    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


def scan(session, kinds, stats):
    seen = {}
    for kind in kinds:
        root = fetch(session, f"/-/en/gp/{kind}/")
        if root is None:
            stats["failed"] += 1
            continue
        stats["ok"] += 1
        seen.update(parse_items(root))
        cats = category_paths(root, kind)[:MAX_CATEGORIES]
        stats["cats"][kind] = len(cats)
        for path in cats:
            time.sleep(random.uniform(*DELAY))
            page = fetch(session, path)
            if page is None:
                stats["failed"] += 1
                continue
            stats["ok"] += 1
            seen.update(parse_items(page))
    return seen


def process(db, seen, stats):
    today = cairo_today()
    alerts = [a for asin, item in seen.items()
              if (a := check_item(db, asin, item, today))]
    forget_old(db, today)
    for a in sorted(alerts, key=lambda x: x["price"] / x["usual"]):
        send_telegram(alert_text(a))
        time.sleep(1)

    total = stats["ok"] + stats["failed"]
    blocked = bool(total) and stats["failed"] / total > 0.5
    if blocked and db["meta"].get("block_notice") != today:
        db["meta"]["block_notice"] = today
        send_telegram(f"⚠️ Amazon blocked {stats['failed']} of {total} pages. "
                      "The bot will slow down and keep trying.")

    m = db["meta"]
    m["last_check"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    m["last_pages_ok"], m["last_pages_failed"] = stats["ok"], stats["failed"]
    m["categories"] = stats["cats"]
    m["checks_today"] = (m.get("checks_today", 0) + 1) if m.get("checks_day") == today else 1
    m["checks_day"] = today
    m["alerts_total"] = m.get("alerts_total", 0) + len(alerts)
    m["telegram_errors"] = TG_ERRORS[-5:]
    save_db(db)
    print(f"{m['last_check']} pages ok={stats['ok']} failed={stats['failed']} "
          f"items={len(seen)} tracked={len(db['items'])} alerts={len(alerts)}", flush=True)
    return blocked


def git_save(message="Update prices"):
    import subprocess
    for cmd in (["git", "add", DATA_FILE],
                ["git", "commit", "-q", "-m", message],
                ["git", "pull", "-q", "--rebase"],
                ["git", "push", "-q"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode and cmd[1] != "commit":     # "nothing to commit" is fine
            print("[git]", " ".join(cmd), r.stderr[-300:], flush=True)
            return


# ---------------- Main ----------------
def main():
    # Test mode: only send a test message and record the result.
    if os.getenv("TEST_ONLY", "").lower() == "true":
        ok = send_telegram("✅ Amazon.eg price bot is working. "
                           f"It checks every {FAST_EVERY:.0f} minutes and alerts on "
                           f"drops of {DROP_PERCENT:.0f}% or more.")
        with open("telegram_test.json", "w") as f:
            json.dump({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "ok": ok, "errors": TG_ERRORS[-3:],
                       "token_shape_ok": bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TOKEN)),
                       "chat_id_shape_ok": bool(re.fullmatch(r"-?\d+", CHAT_ID))}, f)
        return 0 if ok else 1

    db = load_db()
    session = requests.Session()
    start = time.time()
    last_slow = -1e18
    last_save = time.time()

    while True:
        cycle_start = time.time()
        kinds = list(FAST_KINDS)
        if cycle_start - last_slow >= SLOW_EVERY * 60:
            kinds += SLOW_KINDS
            last_slow = cycle_start
        stats = {"ok": 0, "failed": 0, "cats": {}}
        seen = scan(session, kinds, stats)
        blocked = process(db, seen, stats)

        if RUN_MINUTES <= 0:
            break
        if time.time() - last_save >= SAVE_EVERY * 60:
            git_save()
            last_save = time.time()

        wait = FAST_EVERY * 60 - (time.time() - cycle_start)
        if blocked:
            wait = max(wait, 10 * 60)              # back off when Amazon blocks
            session = requests.Session()
        if time.time() + max(wait, 0) - start > RUN_MINUTES * 60:
            break
        time.sleep(max(wait, 5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
