# Amazon.eg deal sniper

Checks Amazon.eg every minute and sends Telegram alerts with one-tap **Add to cart** buttons.

**Alerts**
- 🔥 Price drop – 50%+ below the usual price
- 🏷️ Amazon discount – Amazon's own badge shows 50%+ off
- 🚀 Hot deal – item shooting up Movers & Shakers (selling fast)
- 👀 Watchlist – your own items: price drop, target price, back in stock
- ⚡ Device / 🛒 Supermarket labels for priority sections

**Telegram commands**
- `/watch <amazon link> <target price>` – watch an item every minute
- `/list` – your watchlist
- `/unwatch <number>` – stop watching
- `/status` – is the bot running?
- `/help`

Settings are in `.github/workflows/price-bot.yml` (DROP_PERCENT, FAST_EVERY, ...).
Setup steps: see SETUP.md.
