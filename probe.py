"""One-off: run the bot's own readers on real Amazon.eg pages."""
import json, requests, bot
s = requests.Session()
out = {"products": {}, "lists": {}}
for asin in ["B0B34QF2S2", "B0BZPJ9R6S", "B0CHFVBQ23", "B0BF5BLPG2", "B0DH8JZ2Z4", "B0CVW9DPCB"]:
    page = bot.fetch(s, f"/-/en/dp/{asin}")
    out["products"][asin] = bot.parse_product(page) if page else "FETCH FAILED"
for path in ["/-/en/gp/bestsellers/grocery/", "/-/en/gp/bestsellers/electronics/?pg=2"]:
    page = bot.fetch(s, path)
    items = bot.parse_items(page) if page else {}
    out["lists"][path] = {"count": len(items), "sample": dict(list(items.items())[:4])}
json.dump(out, open("probe.json", "w"), indent=1, ensure_ascii=False)
