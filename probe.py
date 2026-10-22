"""One-off test: can a simple bot read noon.com Egypt prices?"""
import json, re, requests
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
URLS = [
    "https://www.noon.com/egypt-en/",
    "https://www.noon.com/egypt-en/electronics-and-mobiles/mobiles-and-accessories/mobiles-20905/smartphones/eg-all-mobiles/?sort%5Bby%5D=popularity&sort%5Bdir%5D=desc",
    "https://www.noon.com/egypt-en/home-and-kitchen/",
    "https://www.noon.com/egypt-en/minutes/",
    "https://minutes.noon.com/egypt-en/",
    "https://www.noon.com/egypt-en/grocery-store/",
]
out = []
for u in URLS:
    r = {"url": u}
    try:
        res = requests.get(u, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                       "Accept": "text/html,application/xhtml+xml"}, timeout=30)
        t = res.text
        r.update(status=res.status_code, final=res.url, size=len(t),
                 blocked=bool(re.search(r"access denied|captcha|are you a robot|akamai", t, re.I)),
                 next_data="__NEXT_DATA__" in t, egp=len(re.findall(r"EGP", t)),
                 price_keys=len(re.findall(r'"(?:sale_price|price|was_price|offer_price)"\s*:', t)),
                 skus=len(set(re.findall(r'"sku"\s*:\s*"([A-Z0-9]{8,})"', t))),
                 names=re.findall(r'"name"\s*:\s*"([^"]{8,80})"', t)[:4])
        m = re.search(r'"sale_price"|"was_price"|"price"\s*:', t)
        r["snippet"] = t[max(0, m.start() - 400): m.start() + 800] if m else t[:600]
    except Exception as e:
        r["error"] = str(e)[:200]
    out.append(r)
json.dump(out, open("probe.json", "w"), indent=1, ensure_ascii=False)
