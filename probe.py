"""One-off: dump real product cards so the price reader can be fixed."""
import json, re, requests
from bs4 import BeautifulSoup
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
WANT = {"B0B34QF2S2", "B08WJJMFTL", "B0BZPJ9R6S", "B0D4ZC72H8", "B0CHFVBQ23", "B08WJLDSS9"}
URLS = ["/-/en/gp/movers-and-shakers/", "/-/en/gp/movers-and-shakers/electronics/",
        "/-/en/gp/movers-and-shakers/electronics/?ie=UTF8", "/gp/movers-and-shakers/grocery/",
        "/-/en/dp/B0B34QF2S2", "/-/en/dp/B0BZPJ9R6S"]
OLD = ["/-/en/gp/bestsellers/grocery/", "/-/en/gp/movers-and-shakers/grocery/",
        "/-/en/gp/bestsellers/health/", "/-/en/gp/movers-and-shakers/health/",
        "/-/en/gp/bestsellers/beauty/", "/-/en/gp/bestsellers/baby-products/",
        "/-/en/gp/bestsellers/grocery/?pg=2", "/-/en/gp/bestsellers/electronics/"]
out = {"pages": [], "cards": []}
s = requests.Session()
for u in URLS:
    r = s.get("https://www.amazon.eg" + u, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select('[id="gridItemRoot"]') or soup.select("div[data-asin]")
    if "/dp/" in u:
        keep = {}
        for sel in ("#productTitle", "#corePrice_feature_div", "#corePriceDisplay_desktop_feature_div",
                    "#apex_desktop", "#availability", "#buybox", "#outOfStock", "#olpLinkWidget_feature_div",
                    "#price_inside_buybox", "#tp_price_block_total_price_ww"):
            el = soup.select_one(sel)
            keep[sel] = el.get_text(" | ", strip=True)[:300] if el else None
        out["cards"].append({"url": u, "dp": keep, "offscreen": [e.get_text() for e in soup.select(".a-offscreen")][:12]})
    out["pages"].append({"url": u, "status": r.status_code, "final": r.url, "cards": len(cards), "size": len(r.text),
                         "title": soup.title.get_text(strip=True)[:80] if soup.title else None,
                         "asins": len(set(re.findall(r'data-asin="([A-Z0-9]{10})"', r.text))),
                         "lang": (soup.html or {}).get("lang") if soup.html else None,
                         "price_classes": sorted({c for el in soup.select("[class*=price]") for c in el.get("class", [])})[:40]})
    for i, c in enumerate(cards):
        asin = (c.select_one("[data-asin]") or {}).get("data-asin") if c.select_one("[data-asin]") else None
        if asin in WANT or i < 2:
            out["cards"].append({"url": u, "asin": asin, "text": c.get_text(" | ", strip=True)[:500],
                                 "html": str(c)[:6000]})
json.dump(out, open("probe.json", "w"), indent=1, ensure_ascii=False)
