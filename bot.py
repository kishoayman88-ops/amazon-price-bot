"""
Amazon.eg deal sniper.

Runs as a long loop on GitHub Actions and sends Telegram alerts the moment a deal shows up:
  🔥 Price drop      - price is DROP_PERCENT+ below the item's usual price
  🏷️ Amazon discount - Amazon's own badge shows DROP_PERCENT+ off (works on first sight)
  🚀 Hot deal        - item shoots up Movers & Shakers (selling fast) and isn't pricier than usual
  👀 Watchlist       - items you add with /watch, checked every minute

Where it looks:
  every minute  - Movers & Shakers main page + device & supermarket categories,
                  a few other categories and sub-categories in rotation, your watchlist
  every 30 min  - all Best Sellers categories (also learns the sub-categories)

Telegram commands (only from your chat): /watch /unwatch /list /status /help
Prices and settings are kept in prices.json, saved back to GitHub every 30 minutes.
"""

import html
import json
import os
import random
import re
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ---------------- Settings ----------------
BASE = "https://www.amazon.eg"
DATA_FILE = "prices.json"

DROP_PERCENT = float(os.getenv("DROP_PERCENT", "50"))
HOT_FROM_RANK = int(os.getenv("HOT_FROM_RANK", "30"))    # hot deal: was #30 or lower...
HOT_TO_RANK = int(os.getenv("HOT_TO_RANK", "5"))         # ...now in the top 5
HOT_MIN_DROP = float(os.getenv("HOT_MIN_DROP", "20"))    # ...and at least 20% below usual
HOT_MIN_DISC = float(os.getenv("HOT_MIN_DISC", "30"))    # (or Amazon -30% if no history yet)
STABLE_RATIO = 1.6       # history must be steady (max <= 1.6 x min) to trust the usual price
MAX_CONFIRMS = 6         # product-page checks per round (stops alert storms)
WATCH_DROP = float(os.getenv("WATCH_DROP", "5"))         # watchlist: alert on any drop of 5%+
MAX_CATEGORIES = int(os.getenv("MAX_CATEGORIES", "40"))
PRIORITY_BATCH = int(os.getenv("DEVICE_BATCH", "4"))     # priority sub-pages per check
ROTATE_BATCH = int(os.getenv("ROTATE_BATCH", "4"))       # other categories per check
WATCH_BATCH = int(os.getenv("WATCH_BATCH", "10"))        # watchlist items per check
WATCH_MAX = 30
MAX_PRIORITY_PAGES = int(os.getenv("MAX_DEVICE_PAGES", "400"))

RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0"))       # 0 = one full pass, then stop
FAST_EVERY = float(os.getenv("FAST_EVERY", "1"))
SLOW_EVERY = float(os.getenv("SLOW_EVERY", "30"))
SAVE_EVERY = 30                                          # minutes between saves to GitHub

MIN_SAMPLES = 4          # earlier prices (hours or days) needed before a drop alert
KEEP_DAYS = 30
KEEP_HOURS = 24
DELAY = (1.0, 2.0)       # seconds between pages

DEVICE_WORDS = ("electronic", "computer", "appliance", "kitchen", "phone",
                "tv", "camera", "video-game", "office-product")
MARKET_WORDS = ("grocery", "health", "beauty", "baby", "pet", "household")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_ERRORS = []

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

PRICE_RE = re.compile(r"EGP\s*([\d,]+(?:\.\d{1,2})?)")
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})")
CAT_RE = re.compile(r"/gp/(bestsellers|movers-and-shakers)/([a-z0-9][a-z0-9\-_]*)")
CAIRO = timezone(timedelta(hours=3))


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cairo_today():
    return datetime.now(CAIRO).date().isoformat()


# ---------------- Telegram ----------------
def tg(method, **data):
    if not TOKEN:
        TG_ERRORS.append("missing secret: TELEGRAM_BOT_TOKEN")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                          json=data, timeout=data.get("timeout", 0) + 20)
        if r.status_code != 200:
            TG_ERRORS.append(f"{method} {r.status_code}: {r.text[:200]}")
            print("[telegram]", TG_ERRORS[-1], flush=True)
            return None
        return r.json().get("result")
    except requests.RequestException as e:
        TG_ERRORS.append(f"{method} network: " + str(e).replace(TOKEN, "***")[:200])
        return None


def send_telegram(text, asin=None):
    if not CHAT_ID:
        TG_ERRORS.append("missing secret: TELEGRAM_CHAT_ID")
        print("[telegram]", text, flush=True)
        return False
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": False}
    if asin:   # one-tap buttons
        data["reply_markup"] = {"inline_keyboard": [[
            {"text": "🛒 Add to cart",
             "url": f"{BASE}/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"},
            {"text": "🔗 Open", "url": f"{BASE}/dp/{asin}"},
        ]]}
    return tg("sendMessage", **data) is not None


# ---------------- Fetching & parsing ----------------
def is_blocked(page):
    low = page.lower()
    return ("validatecaptcha" in low or "type the characters you see" in low
            or "sorry, we just need to make sure you're not a robot" in low)


def fetch(session, path):
    url = path if path.startswith("http") else BASE + path
    for attempt in range(2):
        try:
            r = session.get(url, headers={"User-Agent": USER_AGENT,
                                          "Accept-Language": "en-US,en;q=0.9",
                                          "Accept": "text/html,application/xhtml+xml"},
                            timeout=30)
            if r.status_code == 200 and not is_blocked(r.text):
                return r.text
            print(f"  blocked/err {r.status_code} on {url}", flush=True)
        except requests.RequestException as e:
            print(f"  network error on {url}: {e}", flush=True)
        time.sleep(4 * (attempt + 1))
    return None


def category_paths(page, kind):
    found = []
    for k, slug in CAT_RE.findall(page):
        path = f"/-/en/gp/{kind}/{slug}/"
        if k == kind and not slug.startswith("ref") and path not in found:
            found.append(path)
    return found


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


def price_from_parts(el):
    """Amazon a-price block -> float, using whole + fraction (offscreen text can lose the dot)."""
    whole = el.select_one(".a-price-whole")
    if whole:
        w = re.sub(r"[^\d]", "", whole.get_text())
        frac = el.select_one(".a-price-fraction")
        f = re.sub(r"[^\d]", "", frac.get_text()) if frac else "0"
        if w:
            return float(f"{w}.{f or 0}")
    return parse_price(el.get_text(" ", strip=True))


def parse_items(page):
    """Best Sellers page -> {asin: {title, price, rank}}.
    Only the main buy-box price is used. Cards showing 'N offers from ...' (other sellers,
    usually when the main listing is out of stock) are skipped - they are not the real price."""
    soup = BeautifulSoup(page, "html.parser")
    items = {}
    for card in soup.select('[id="gridItemRoot"]'):
        inner = card.select_one("[data-asin]")
        asin = inner.get("data-asin") if inner else None
        if not asin:
            link = card.find("a", href=ASIN_RE)
            m = ASIN_RE.search(link["href"]) if link else None
            asin = m.group(1) if m else None
        if not asin or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        el = card.select_one('[class*="p13n-sc-price_"]') or card.select_one(".p13n-sc-price")
        price = parse_price(el.get_text(" ", strip=True)) if el else None
        if price is None:
            continue
        text = card.get_text(" ", strip=True)
        img = card.find("img", alt=True)
        title = img["alt"].strip() if img and img["alt"].strip() else text[:120]
        rank = re.match(r"#(\d+)", text)
        items[asin] = {"title": title[:200], "price": price,
                       "rank": int(rank.group(1)) if rank else None}
    return items


def parse_product(page):
    """Product page -> {title, price, list_price, disc, in_stock, seller}."""
    soup = BeautifulSoup(page, "html.parser")
    t = soup.select_one("#productTitle")
    out = {"title": t.get_text(" ", strip=True)[:200] if t else "", "price": None,
           "list_price": None, "disc": None, "in_stock": False, "seller": None}
    for sel in ("#corePriceDisplay_desktop_feature_div .priceToPay",
                "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price)",
                "#corePrice_feature_div .a-price:not(.a-text-price)",
                "#apex_desktop .a-price:not(.a-text-price)"):
        el = soup.select_one(sel)
        if el and (v := price_from_parts(el)):
            out["price"] = v
            break
    core = soup.select_one("#corePriceDisplay_desktop_feature_div") or soup.select_one("#apex_desktop")
    core_text = core.get_text(" ", strip=True) if core else ""
    lp = re.search(r"List Price:\s*EGP\s*([\d,]+\.\d{2})", core_text)
    if lp:
        out["list_price"] = float(lp.group(1).replace(",", ""))
    d = re.search(r"-\s?(\d{1,2})\s?%", core_text)
    if d:
        out["disc"] = int(d.group(1))
    box = soup.select_one("#buybox")
    box_text = box.get_text(" ", strip=True).lower() if box else ""
    avail = soup.select_one("#availability")
    avail_text = avail.get_text(" ", strip=True).lower() if avail else ""
    unavailable = "currently unavailable" in avail_text or "out of stock" in avail_text
    out["in_stock"] = bool(out["price"]) and not unavailable and (
        "in stock" in box_text or "in stock" in avail_text or bool(soup.select_one("#add-to-cart-button")))
    sold = re.search(r"sold by:?\s*([^|]{2,40}?)(?:\s{2,}|$| free| returns| payment)", box_text)
    if sold:
        out["seller"] = sold.group(1).strip().title()
    return out


def group_of(slug):
    if any(w in slug for w in DEVICE_WORDS):
        return "device"
    if any(w in slug for w in MARKET_WORDS):
        return "market"
    return None


# ---------------- Storage ----------------
def load_db():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}
    db.setdefault("items", {})
    db.setdefault("meta", {})
    db.setdefault("watch", {})
    return db


def save_db(db):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))


def git_save(message="Update prices"):
    for cmd in (["git", "add", DATA_FILE], ["git", "commit", "-q", "-m", message],
                ["git", "pull", "-q", "--rebase"], ["git", "push", "-q"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode and cmd[1] != "commit":
            print("[git]", " ".join(cmd), r.stderr[-300:], flush=True)
            return


# ---------------- Deal logic ----------------
def check_item(db, asin, item, today, list_key=""):
    """Record a buy-box price; return a deal *candidate* (confirmed later) or None."""
    rec = db["items"].setdefault(asin, {"t": item["title"], "h": {}, "alerted": None})
    rec["t"] = item["title"]
    rec["seen"] = today
    price = item["price"]
    hour = datetime.now(CAIRO).strftime("%Y-%m-%dT%H")
    recent = rec.setdefault("r", {})

    past = ([p for d, p in rec["h"].items() if d != today]
            + [p for h, p in recent.items() if h != hour])
    usual = statistics.median(past) if len(past) >= MIN_SAMPLES else None
    stable = bool(past) and max(past) <= min(past) * STABLE_RATIO   # usual price is trustworthy
    base = {"asin": asin, "title": item["title"], "group": item.get("group"),
            "price": price, "usual": usual if stable else None,
            "lowest": min(past) if past else None, "samples": len(past)}
    cand = None

    if usual and stable:                                          # 1) price drop
        if price <= usual * (1 - DROP_PERCENT / 100):
            if rec.get("alerted") is None or price < rec["alerted"] * 0.98:
                cand = dict(base, type="drop")
        else:
            rec["alerted"] = None

    rank = item.get("rank")                                       # 2) hot: rank jump
    if rank and list_key:
        ranks = rec.setdefault("rk", {})
        prev = ranks.get(list_key)
        ranks[list_key] = rank
        if (not cand and prev and prev >= HOT_FROM_RANK and rank <= HOT_TO_RANK
                and rec.get("hd") != today):
            cand = dict(base, type="hot", prev_rank=prev, rank=rank)

    rec["h"][today] = price
    recent[hour] = price
    for d in sorted(rec["h"])[:-KEEP_DAYS]:
        del rec["h"][d]
    for h in sorted(recent)[:-KEEP_HOURS]:
        del recent[h]
    if cand and rec.get("nochk") == hour:     # confirmed as not-a-deal this hour already
        return None
    return cand


def pct_below(price, ref):
    return round((1 - price / ref) * 100)


def alert_text(a):
    tag = {"device": "⚡ <b>Device</b> · ", "market": "🛒 <b>Supermarket</b> · "}.get(a.get("group"), "")
    if a["type"] == "drop":
        head = f"🔥 <b>Price drop {pct_below(a['price'], a['usual'])}%</b>"
    elif a["type"] == "hot":
        head = f"🚀 <b>Hot deal</b> · jumped #{a['prev_rank']} → #{a['rank']} in Best Sellers"
    else:
        head = a["head"]
    lines = [f"{tag}{head}", html.escape(a["title"]), "",
             f"Now: <b>EGP {a['price']:,.2f}</b>" + ("  ✅ checked" if a.get("confirmed") else "")]
    if a.get("usual") and a["type"] != "watch":
        lines.append(f"Usual (bot history): EGP {a['usual']:,.2f} (-{pct_below(a['price'], a['usual'])}%)")
    if a.get("lowest") and a["lowest"] > 0:
        lines.append(f"Lowest seen before: EGP {a['lowest']:,.2f}")
    if a.get("list_price") and a["list_price"] > a["price"]:
        lines.append(f"Amazon list price: EGP {a['list_price']:,.2f} (-{pct_below(a['price'], a['list_price'])}%)")
    if a.get("seller"):
        lines.append(f"Sold by: {html.escape(a['seller'])}")
    lines.append(f"{BASE}/dp/{a['asin']}")
    return "\n".join(lines)


class Sniper:
    def __init__(self, db):
        self.db = db
        self.session = requests.Session()
        self.slugs = []                 # top-level categories (from Best Sellers)
        self.priority_pages = []        # device & supermarket sub-category pages
        self.other_pages = []           # sub-category pages of all other categories
        self.page2 = []                 # page 2 of the busiest lists
        self.empty = {}                 # path -> times in a row with no priced items
        self.pos = {"pri": 0, "rot": 0, "watch": 0, "oth": 0, "pg2": 0}
        self.stats = None
        self.alerts = 0

    # -- emit: check items the moment a page is read; confirm candidates, alert instantly --
    def emit(self, items, group=None, list_key=""):
        today = cairo_today()
        for asin, item in items.items():
            if group:
                item["group"] = group
            cand = check_item(self.db, asin, item, today, list_key)
            if cand and self.stats["confirms"] < MAX_CONFIRMS:
                self.confirm(cand)
        self.stats["items"] += len(items)

    def confirm(self, c):
        """Open the product page and alert only if the deal is real and in stock."""
        self.stats["confirms"] += 1
        rec = self.db["items"][c["asin"]]
        page = self.get(f"/-/en/dp/{c['asin']}")
        if not page:
            return
        p = parse_product(page)
        price, ok = p["price"], False
        if p["in_stock"] and price:
            if c["type"] == "drop":
                ok = price <= c["usual"] * (1 - DROP_PERCENT / 100)
            else:   # hot: selling fast AND clearly cheaper than usual (or a real Amazon discount)
                ok = ((c.get("usual") and price <= c["usual"] * (1 - HOT_MIN_DROP / 100))
                      or (not c.get("usual") and (p["disc"] or 0) >= HOT_MIN_DISC))
        hour = datetime.now(CAIRO).strftime("%Y-%m-%dT%H")
        if not ok:
            rec["nochk"] = hour
            return
        c.update(price=price, confirmed=True, list_price=p["list_price"], seller=p["seller"])
        if c["type"] == "drop":
            rec["alerted"] = price
        else:
            rec["hd"] = cairo_today()
        send_telegram(alert_text(c), c["asin"])
        self.alerts += 1

    def get(self, path):
        time.sleep(random.uniform(*DELAY))
        page = fetch(self.session, path)
        self.stats["ok" if page else "failed"] += 1
        return page

    def read(self, path, group=None):
        page = self.get(path)
        if page:
            items = parse_items(page)
            key = path.split("/gp/bestsellers/")[-1].strip("/")
            self.emit(items, group, key)
            self.empty[path] = 0 if items else self.empty.get(path, 0) + 1
            if self.empty[path] >= 3:              # stop wasting requests on empty pages
                for lst in (self.priority_pages, self.other_pages, self.page2):
                    if path in lst:
                        lst.remove(path)
        return page

    def learn(self, page, slug):
        lst = self.priority_pages if group_of(slug) else self.other_pages
        cap = MAX_PRIORITY_PAGES if group_of(slug) else MAX_PRIORITY_PAGES // 2
        for node in re.findall(rf"/gp/bestsellers/{re.escape(slug)}/(\d+)", page):
            p = f"/-/en/gp/bestsellers/{slug}/{node}/"
            if p not in lst and len(lst) < cap and self.empty.get(p, 0) < 3:
                lst.append(p)

    def rotate(self, lst, key, n):
        for _ in range(min(n, len(lst))):
            if not lst:
                return
            path = lst[self.pos[key] % len(lst)]
            self.pos[key] += 1
            slug = path.split("/")[5]
            page = self.read(path, group_of(slug))
            if page and "?pg=" not in path:
                self.learn(page, slug)

    # -- every 30 min: every Best Sellers category, pages 1 and 2 --
    def scan_slow(self):
        root = self.get("/-/en/gp/bestsellers/")
        if not root:
            return
        cats = category_paths(root, "bestsellers")[:MAX_CATEGORIES]
        if cats:
            self.slugs = [c.rstrip("/").split("/")[-1] for c in cats]
        for path in cats:
            slug = path.rstrip("/").split("/")[-1]
            if group_of(slug):
                continue                               # priority ones are read every minute
            page = self.read(path)
            if page:
                self.learn(page, slug)
            if self.empty.get(path + "?pg=2", 0) < 3:
                self.read(path + "?pg=2")
        self.page2 = [f"/-/en/gp/bestsellers/{s}/?pg=2" for s in self.slugs
                      if group_of(s) and self.empty.get(f"/-/en/gp/bestsellers/{s}/?pg=2", 0) < 3]

    # -- every minute: devices & supermarket first, then rotation --
    def scan_fast(self):
        pri = [s for s in self.slugs if group_of(s)]
        other = [s for s in self.slugs if not group_of(s)]
        for slug in pri:
            page = self.read(f"/-/en/gp/bestsellers/{slug}/", group_of(slug))
            if page:
                self.learn(page, slug)
        self.rotate(self.page2, "pg2", 2)
        self.rotate(self.priority_pages, "pri", PRIORITY_BATCH)
        for _ in range(min(ROTATE_BATCH, len(other))):
            slug = other[self.pos["rot"] % len(other)]
            self.pos["rot"] += 1
            self.read(f"/-/en/gp/bestsellers/{slug}/")
        self.rotate(self.other_pages, "oth", 2)
        self.stats["cats"] = {"priority": pri, "other": len(other),
                              "priority_sub_pages": len(self.priority_pages),
                              "other_sub_pages": len(self.other_pages),
                              "skipped_empty": sum(1 for v in self.empty.values() if v >= 3)}

    # -- watchlist --
    def scan_watch(self):
        watch = self.db["watch"]
        asins = list(watch)
        for _ in range(min(WATCH_BATCH, len(asins))):
            asin = asins[self.pos["watch"] % len(asins)]
            self.pos["watch"] += 1
            page = self.get(f"/-/en/dp/{asin}")
            if not page:
                continue
            p = parse_product(page)
            title, price, in_stock = p["title"], p["price"], p["in_stock"]
            w = watch[asin]
            w["t"] = title or w.get("t", asin)
            last, was_in = w.get("last"), w.get("in_stock", True)
            alert = None
            if in_stock and not was_in:
                alert = "✅ <b>Back in stock</b>"
            elif in_stock and price and w.get("target") and price <= w["target"] \
                    and (w.get("alerted") is None or price < w["alerted"]):
                alert = f"🎯 <b>Target price hit</b> (target EGP {w['target']:,.0f})"
                w["alerted"] = price
            elif in_stock and price and last and price <= last * (1 - WATCH_DROP / 100):
                alert = f"📉 <b>Price down {pct_below(price, last)}%</b> (was EGP {last:,.2f})"
            if alert and price:
                send_telegram(alert_text({"type": "watch", "head": "👀 " + alert, "asin": asin,
                                          "title": w["t"], "price": price,
                                          "lowest": w.get("low")}), asin)
                self.alerts += 1
            if price:
                w["last"] = price
                w["low"] = min(price, w.get("low") or price)
            w["in_stock"] = in_stock


# ---------------- Telegram commands ----------------
HELP = ("🤖 <b>Amazon.eg deal sniper</b>\n"
        "/watch &lt;amazon link&gt; [target price] – watch an item every minute\n"
        "/unwatch &lt;number or link&gt; – stop watching\n"
        "/list – your watchlist\n"
        "/status – is the bot running?\n"
        "/help – this message")


def find_asin(text):
    """ASIN from an Amazon link, a bare ASIN, or a short share link (amzn.eu/d/..., a.co/...)."""
    m = ASIN_RE.search(text) or re.search(r"\b(B0[A-Z0-9]{8})\b", text)
    if m:
        return m.group(1)
    for url in re.findall(r"https?://\S+", text):
        if re.search(r"amzn\.|a\.co/|amazon\.", url):
            try:
                r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15,
                                 allow_redirects=True)
                m = ASIN_RE.search(r.url) or ASIN_RE.search(r.text[:200000])
                if m:
                    return m.group(1)
            except requests.RequestException:
                pass
    return None


def handle_command(db, text, sniper, speed):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    watch = db["watch"]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        m = db["meta"]
        return (f"✅ Running. Checking every {speed:g} min.\n"
                f"Checks today: {m.get('checks_today', 0)}\n"
                f"Products tracked: {len(db['items']):,}\n"
                f"Last check: pages ok {m.get('last_pages_ok', 0)}, failed {m.get('last_pages_failed', 0)}\n"
                f"Watchlist: {len(watch)} items\nAlerts sent (total): {m.get('alerts_total', 0)}")
    if cmd == "/list":
        if not watch:
            return "Your watchlist is empty. Send /watch &lt;link&gt; &lt;target price&gt;"
        rows = []
        for i, (a, w) in enumerate(watch.items(), 1):
            tgt = f" · target EGP {w['target']:,.0f}" if w.get("target") else ""
            now = f"EGP {w['last']:,.2f}" if w.get("last") else "checking..."
            rows.append(f"{i}. {html.escape(w.get('t', a)[:60])}\n   {now}{tgt}")
        return "👀 <b>Watchlist</b>\n" + "\n".join(rows)
    if cmd == "/watch":
        asin = find_asin(text)
        if not asin:
            return ("Send it like this:\n/watch https://www.amazon.eg/dp/B0XXXXXXXX 1500\n"
                    "(the number is your target price, optional)")
        if len(watch) >= WATCH_MAX:
            return f"Watchlist is full ({WATCH_MAX}). Remove one with /unwatch."
        rest = re.sub(r"https?://\S+|\bB0[A-Z0-9]{8}\b", " ", text)
        nums = [float(n.replace(",", "")) for n in re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])", rest)]
        target = nums[-1] if nums else None
        watch[asin] = {"target": target, "t": asin, "in_stock": True}
        save_db(db)
        return (f"👀 Watching {asin}" + (f" · target EGP {target:,.0f}" if target else "")
                + ".\nI'll alert you on price drops, target price and back in stock.")
    if cmd == "/unwatch":
        key = find_asin(text) if "http" in text or "B0" in text else None
        if not key and len(parts) > 1 and parts[1].isdigit():
            idx = int(parts[1]) - 1
            key = list(watch)[idx] if 0 <= idx < len(watch) else None
        if key and key in watch:
            del watch[key]
            save_db(db)
            return "🗑️ Removed from watchlist."
        return "Not found. Use /list to see the numbers."
    return None


def poll_commands(db, sniper, speed, wait):
    """Answer commands; wait up to `wait` seconds (long polling = instant replies)."""
    if not TOKEN:
        time.sleep(max(wait, 0))
        return
    end = time.time() + max(wait, 0)
    while True:
        left = max(0, int(end - time.time()))
        updates = tg("getUpdates", offset=db["meta"].get("tg_offset", 0),
                     timeout=min(left, 50), allowed_updates=["message"])
        if updates is None:                      # Telegram problem -> don't spin
            time.sleep(min(max(left, 1), 10))
            updates = []
        for u in updates:
            db["meta"]["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != CHAT_ID or not msg.get("text"):
                continue
            reply = handle_command(db, msg["text"], sniper, speed)
            if reply:
                send_telegram(reply)
        if time.time() >= end:
            return
        if not updates and left == 0:
            return


# ---------------- Main ----------------
def main():
    if os.getenv("TEST_ONLY", "").lower() == "true":
        ok = send_telegram("✅ Amazon.eg deal sniper is working. "
                           f"It checks every {FAST_EVERY:g} minute(s). Send /help for commands.")
        with open("telegram_test.json", "w") as f:
            json.dump({"time": now_utc(), "ok": ok, "errors": TG_ERRORS[-3:],
                       "token_shape_ok": bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TOKEN)),
                       "chat_id_shape_ok": bool(re.fullmatch(r"-?\d+", CHAT_ID))}, f)
        return 0 if ok else 1

    db = load_db()
    if db["meta"].get("schema") != 2:          # old history mixed in wrong prices -> start clean
        db["items"] = {}
        db["meta"]["schema"] = 2
    sniper = Sniper(db)
    tg("setMyCommands", commands=[            # shows the command menu in Telegram
        {"command": "watch", "description": "Watch an item: /watch <link> <target price>"},
        {"command": "list", "description": "Show your watchlist"},
        {"command": "unwatch", "description": "Stop watching: /unwatch <number>"},
        {"command": "status", "description": "Is the bot running?"},
        {"command": "help", "description": "How to use the bot"}])
    start, last_slow, last_save = time.time(), -1e18, time.time()
    speeds = [FAST_EVERY, 2, 3, 5, 10, 20]          # adaptive speed when Amazon blocks
    level, clean = db["meta"].get("speed_level", 0), 0
    if level >= len(speeds):
        level = 0

    while True:
        cycle_start = time.time()
        sniper.stats = {"ok": 0, "failed": 0, "items": 0, "cats": {}, "confirms": 0}
        sniper.alerts = 0
        if cycle_start - last_slow >= SLOW_EVERY * 60:
            sniper.scan_slow()
            last_slow = cycle_start
        sniper.scan_fast()
        sniper.scan_watch()

        st, today, m = sniper.stats, cairo_today(), db["meta"]
        total = st["ok"] + st["failed"]
        fail_rate = st["failed"] / total if total else 0
        cutoff = (datetime.now(CAIRO) - timedelta(days=KEEP_DAYS)).date().isoformat()
        for a in [a for a, r in db["items"].items() if r.get("seen", today) < cutoff]:
            del db["items"][a]
        m.update(last_check=now_utc(), last_pages_ok=st["ok"], last_pages_failed=st["failed"],
                 categories=st["cats"], telegram_errors=TG_ERRORS[-5:],
                 alerts_total=m.get("alerts_total", 0) + sniper.alerts,
                 checks_today=(m.get("checks_today", 0) + 1) if m.get("checks_day") == today else 1,
                 checks_day=today)
        print(f"{m['last_check']} ok={st['ok']} failed={st['failed']} items={st['items']} "
              f"alerts={sniper.alerts} speed={speeds[level]:g}m", flush=True)

        if RUN_MINUTES <= 0:
            save_db(db)
            break

        if fail_rate > 0.2:                          # being blocked -> slow down
            level, clean = min(level + 1, len(speeds) - 1), 0
            sniper.session = requests.Session()
        elif fail_rate == 0:
            clean += 1
            if level and clean >= 10:                # 10 clean checks -> speed up
                level, clean = level - 1, 0
        old = m.get("speed_level", 0)
        if level != old:
            m["speed_level"] = level
            if level == 0:
                send_telegram(f"✅ Amazon is OK again. Back to checking every {speeds[0]:g} min.")
            elif old == 0:
                send_telegram(f"🐢 Amazon is blocking some checks. Slowing to every "
                              f"{speeds[level]:g} min, will speed up again automatically.")
        save_db(db)

        if time.time() - last_save >= SAVE_EVERY * 60:
            git_save()
            last_save = time.time()

        wait = speeds[level] * 60 - (time.time() - cycle_start)
        if time.time() + max(wait, 0) - start > RUN_MINUTES * 60:
            break
        poll_commands(db, sniper, speeds[level], max(wait, 3))
    save_db(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
