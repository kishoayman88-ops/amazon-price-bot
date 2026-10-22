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
MIN_SAMPLES = 3           # earlier prices needed (hours or days) before a drop alert
KEEP_DAYS = 30            # days of daily prices to keep per item
KEEP_HOURS = 24           # hours of hourly prices to keep per item
DELAY = (1.0, 2.0)        # seconds to wait between pages
HOT_JUMP = float(os.getenv("HOT_JUMP", "2000"))          # Movers & Shakers rank jump %
HOT_MIN_DROP = float(os.getenv("HOT_MIN_DROP", "20"))    # and at least this % below usual
MAX_HOT_PER_CHECK = 5

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
    for attempt in range(2):
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
        time.sleep(4 * (attempt + 1))
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


def parse_jump(text):
    """Movers & Shakers: how much the sales rank went up, in %."""
    cur = re.search(r"Sales rank:\s*([\d,]+)", text)
    prev = re.search(r"previously\s+([\d,]+)", text)
    if cur and prev:
        c, p = int(cur.group(1).replace(",", "")), int(prev.group(1).replace(",", ""))
        return (p / c - 1) * 100 if c else None
    m = re.search(r"(?<![-\d,.])(\d[\d,]*)\s?%", text)
    return float(m.group(1).replace(",", "")) if m else None


def parse_items(page, movers=False):
    """Return {asin: {"title", "price", ...}} from a best-seller style page."""
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
        text = card.get_text(" ", strip=True)
        price = parse_price(text)
        if price is None:
            continue
        img = card.find("img", alt=True)
        title = (img["alt"].strip() if img and img["alt"].strip()
                 else card.get_text(" ", strip=True)[:120])
        item = {"title": title[:200], "price": price}
        disc = re.search(r"-\s?(\d{1,2})\s?%", text)       # Amazon's own discount badge
        if disc:
            item["disc"] = int(disc.group(1))
        if movers:
            item["jump"] = parse_jump(text)
        items[asin] = item
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
    """Update history; return an alert dict or None."""
    rec = db["items"].setdefault(asin, {"t": item["title"], "h": {}, "alerted": None})
    rec["t"] = item["title"]
    rec["seen"] = today
    price = item["price"]
    hour = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H")
    recent = rec.setdefault("r", {})

    past = ([p for d, p in rec["h"].items() if d != today]
            + [p for h, p in recent.items() if h != hour])
    usual = statistics.median(past) if len(past) >= MIN_SAMPLES else None
    base = {"asin": asin, "title": item["title"], "group": item.get("group"),
            "price": price, "usual": usual}
    alert = None

    # 1) Price drop vs the item's usual price
    if usual:
        if price <= usual * (1 - DROP_PERCENT / 100):
            last = rec.get("alerted")
            if last is None or price < last:      # alert again only if even lower
                rec["alerted"] = price
                alert = dict(base, type="drop")
        else:
            rec["alerted"] = None                  # price recovered -> reset

    # 2) Amazon's own discount badge (works from the first time we see the item)
    if not alert and item.get("disc", 0) >= DROP_PERCENT and rec.get("dd") != today:
        rec["dd"] = today
        alert = dict(base, type="disc", disc=item["disc"])

    # 3) Hot deal: shooting up Movers & Shakers (and not more expensive than usual)
    jump = item.get("jump") or 0
    if (not alert and jump >= HOT_JUMP and rec.get("hd") != today
            and (usual is None or price <= usual * (1 - HOT_MIN_DROP / 100))):
        rec["hd"] = today
        alert = dict(base, type="hot", jump=jump)

    rec["h"][today] = price                        # one price per day (latest)
    recent[hour] = price                           # one price per hour (latest)
    for d in sorted(rec["h"])[:-KEEP_DAYS]:
        del rec["h"][d]
    for h in sorted(recent)[:-KEEP_HOURS]:
        del recent[h]
    return alert


def forget_old(db, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    old = [a for a, r in db["items"].items() if r.get("seen", today) < cutoff]
    for a in old:
        del db["items"][a]
    return len(old)


def alert_text(a):
    dev = {"device": "⚡ <b>Device</b> · ", "market": "🛒 <b>Supermarket</b> · "}.get(a.get("group"), "")
    title = html.escape(a["title"])
    link = f"https://www.amazon.eg/dp/{a['asin']}"
    usual = (f"Usual: EGP {a['usual']:,.2f} "
             f"(-{round((1 - a['price'] / a['usual']) * 100)}%)\n") if a.get("usual") else ""
    if a["type"] == "drop":
        head = f"🔥 <b>Price drop {round((1 - a['price'] / a['usual']) * 100)}%</b>"
    elif a["type"] == "disc":
        head = f"🏷️ <b>Amazon discount -{a['disc']}%</b>"
    else:
        head = f"🚀 <b>Hot deal</b> · selling fast (rank +{a['jump']:,.0f}%)"
    return (f"{dev}{head}\n{title}\n\n"
            f"Now: <b>EGP {a['price']:,.2f}</b>\n{usual}{link}")


# ---------------- Loop mode ----------------

SLOW_KINDS = ["bestsellers"]          # checked every SLOW_EVERY minutes
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0"))   # 0 = one full pass, then stop
FAST_EVERY = float(os.getenv("FAST_EVERY", "1"))
SLOW_EVERY = float(os.getenv("SLOW_EVERY", "30"))
SAVE_EVERY = 30                       # minutes between saves to GitHub


def cairo_today():
    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


CAT_SLUGS = []   # category names learned from Best Sellers, reused for Movers & Shakers

# ---- Electric devices: deeper scan of sub-categories ----
DEVICE_WORDS = ("electronic", "computer", "appliance", "kitchen", "phone",
                "tv", "camera", "video-game", "office-product")
DEVICE_BATCH = int(os.getenv("DEVICE_BATCH", "4"))        # device sub-pages per check
ROTATE_BATCH = int(os.getenv("ROTATE_BATCH", "4"))        # other categories per check
ROTATE_POS = [0]
MAX_DEVICE_PAGES = int(os.getenv("MAX_DEVICE_PAGES", "300"))
DEVICE_PAGES = []       # rotating list of device sub-category pages
DEVICE_POS = [0]


MARKET_WORDS = ("grocery", "health", "beauty", "baby", "pet", "household")


def group_of(slug):
    """'device', 'market' (supermarket) or None."""
    if any(w in slug for w in DEVICE_WORDS):
        return "device"
    if any(w in slug for w in MARKET_WORDS):
        return "market"
    return None


def is_device(slug):          # any priority group (devices or supermarket)
    return group_of(slug) is not None


def learn_device_pages(page, slug):
    """Add sub-category pages (Best Sellers + Movers & Shakers) found on a device page."""
    for node in re.findall(rf"/gp/bestsellers/{re.escape(slug)}/(\d+)", page):
        for kind in ("bestsellers", "movers-and-shakers"):
            path = f"/-/en/gp/{kind}/{slug}/{node}/"
            if path not in DEVICE_PAGES and len(DEVICE_PAGES) < MAX_DEVICE_PAGES:
                DEVICE_PAGES.append(path)


def scan_devices(session, stats):
    seen = {}
    n = min(DEVICE_BATCH, len(DEVICE_PAGES))
    for _ in range(n):
        path = DEVICE_PAGES[DEVICE_POS[0] % len(DEVICE_PAGES)]
        DEVICE_POS[0] += 1
        time.sleep(random.uniform(*DELAY))
        page = fetch(session, path)
        if page is None:
            stats["failed"] += 1
            continue
        stats["ok"] += 1
        if "/bestsellers/" in path:          # go one level deeper
            learn_device_pages(page, path.split("/")[5])
        for asin, item in parse_items(page, movers="movers-and-shakers" in path).items():
            item["group"] = group_of(path.split("/")[5])
            seen[asin] = item
    stats["device_pages"] = len(DEVICE_PAGES)
    return seen


def fetch_into(session, path, seen, stats, movers, group):
    time.sleep(random.uniform(*DELAY))
    page = fetch(session, path)
    if page is None:
        stats["failed"] += 1
        return
    stats["ok"] += 1
    for asin, item in parse_items(page, movers=movers).items():
        if group:
            item["group"] = group
        seen[asin] = item


def scan_fast(session, stats):
    """Every minute: Movers & Shakers main page + device categories,
    plus a few other categories and device sub-pages in rotation."""
    seen = {}
    kind = "movers-and-shakers"
    fetch_into(session, f"/-/en/gp/{kind}/", seen, stats, True, None)
    dev = [s for s in CAT_SLUGS if is_device(s)]
    other = [s for s in CAT_SLUGS if not is_device(s)]
    for slug in dev:
        fetch_into(session, f"/-/en/gp/{kind}/{slug}/", seen, stats, True, group_of(slug))
    for _ in range(min(ROTATE_BATCH, len(other))):
        slug = other[ROTATE_POS[0] % len(other)]
        ROTATE_POS[0] += 1
        fetch_into(session, f"/-/en/gp/{kind}/{slug}/", seen, stats, True, None)
    stats["cats"] = {"priority": dev, "other": len(other)}
    seen.update(scan_devices(session, stats))
    return seen


def scan(session, kinds, stats):
    seen = {}
    for kind in kinds:
        root = fetch(session, f"/-/en/gp/{kind}/")
        if root is None:
            stats["failed"] += 1
            continue
        stats["ok"] += 1
        seen.update(parse_items(root, movers=(kind == "movers-and-shakers")))
        cats = category_paths(root, kind)[:MAX_CATEGORIES]
        if kind == "bestsellers" and cats:
            CAT_SLUGS[:] = [c.rstrip("/").split("/")[-1] for c in cats]
        if not cats and CAT_SLUGS:      # Movers & Shakers page hides its category links
            cats = [f"/-/en/gp/{kind}/{slug}/" for slug in CAT_SLUGS]
        stats["cats"][kind] = len(cats)
        for path in cats:
            time.sleep(random.uniform(*DELAY))
            page = fetch(session, path)
            if page is None:
                stats["failed"] += 1
                continue
            stats["ok"] += 1
            slug = path.rstrip("/").split("/")[-1]
            items = parse_items(page, movers=(kind == "movers-and-shakers"))
            if is_device(slug):
                if kind == "bestsellers":
                    learn_device_pages(page, slug)
                for it in items.values():
                    it["group"] = group_of(slug)
            seen.update(items)
    return seen


def process(db, seen, stats):
    today = cairo_today()
    alerts = [a for asin, item in seen.items()
              if (a := check_item(db, asin, item, today))]
    forget_old(db, today)
    order = {"drop": 0, "disc": 1, "hot": 2}
    alerts.sort(key=lambda x: (order[x["type"]], -(x.get("jump") or 0)))
    hot = [a for a in alerts if a["type"] == "hot"]
    alerts = [a for a in alerts if a["type"] != "hot"] + hot[:MAX_HOT_PER_CHECK]
    for a in alerts:
        send_telegram(alert_text(a))
        time.sleep(1)

    total = stats["ok"] + stats["failed"]
    fail_rate = stats["failed"] / total if total else 0

    m = db["meta"]
    m["last_check"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    m["last_pages_ok"], m["last_pages_failed"] = stats["ok"], stats["failed"]
    m["categories"] = stats["cats"]
    m["device_pages"] = stats.get("device_pages", 0)
    m["checks_today"] = (m.get("checks_today", 0) + 1) if m.get("checks_day") == today else 1
    m["checks_day"] = today
    m["alerts_total"] = m.get("alerts_total", 0) + len(alerts)
    m["telegram_errors"] = TG_ERRORS[-5:]
    save_db(db)
    print(f"{m['last_check']} pages ok={stats['ok']} failed={stats['failed']} "
          f"items={len(seen)} tracked={len(db['items'])} alerts={len(alerts)}", flush=True)
    return fail_rate


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
                           f"It checks every {FAST_EVERY:g} minute(s) and alerts on "
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
    # Adaptive speed: slow down when Amazon blocks, speed up again when it's fine.
    speeds = [FAST_EVERY, 2, 3, 5, 10, 20]
    level, clean = 0, 0

    while True:
        cycle_start = time.time()
        stats = {"ok": 0, "failed": 0, "cats": {}}
        seen = {}
        if cycle_start - last_slow >= SLOW_EVERY * 60:   # full Best Sellers pass
            seen.update(scan(session, SLOW_KINDS, stats))
            last_slow = cycle_start
        seen.update(scan_fast(session, stats))
        fail_rate = process(db, seen, stats)

        if RUN_MINUTES <= 0:
            break
        if time.time() - last_save >= SAVE_EVERY * 60:
            git_save()
            last_save = time.time()

        if fail_rate > 0.2:                         # being blocked -> slow down
            level, clean = min(level + 1, len(speeds) - 1), 0
            session = requests.Session()           # fresh cookies
        elif fail_rate == 0:
            clean += 1
            if level and clean >= 10:              # 10 clean checks -> speed up
                level, clean = level - 1, 0
        if level != db["meta"].get("speed_level", 0):
            old = speeds[db["meta"].get("speed_level", 0)]
            db["meta"]["speed_level"] = level
            if level == 0:
                send_telegram(f"✅ Amazon is OK again. Back to checking every {speeds[0]:g} min.")
            elif level > 0 and old == speeds[0]:
                send_telegram(f"🐢 Amazon is blocking some checks. Slowing down to every "
                              f"{speeds[level]:g} min, will speed up again automatically.")
            save_db(db)
        wait = speeds[level] * 60 - (time.time() - cycle_start)
        if time.time() + max(wait, 0) - start > RUN_MINUTES * 60:
            break
        time.sleep(max(wait, 5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
