import json, re, requests
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
URLS = ["https://www.jumia.com.eg/", "https://www.jumia.com.eg/flash-sales/",
        "https://www.jumia.com.eg/phones-tablets/", "https://www.jumia.com.eg/home-office/"]
out = []
for u in URLS:
    r = {"url": u}
    try:
        res = requests.get(u, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                       "Accept": "text/html"}, timeout=30)
        t = res.text
        r.update(status=res.status_code, size=len(t),
                 blocked=bool(re.search(r"captcha|are you a robot|cf-chl|Just a moment", t, re.I)),
                 cards=len(re.findall(r'<article class="prd', t)),
                 prc=len(re.findall(r'class="prc"', t)), old=len(re.findall(r'class="old"', t)),
                 dsct=re.findall(r'class="bdg _dsct[^"]*">([^<]+)<', t)[:5],
                 cats=sorted(set(re.findall(r'href="(/[a-z0-9\-]+/)"', t)))[:60])
        i = t.find('<article class="prd')
        r["snippet"] = t[i:i + 1800] if i >= 0 else t[:400]
    except Exception as e:
        r["error"] = str(e)[:200]
    out.append(r)
json.dump(out, open("probe.json", "w"), indent=1, ensure_ascii=False)
