# Amazon.eg Price Bot — Setup (about 15 minutes, phone is OK)

## Step 1 — Make your Telegram bot
1. In Telegram, open **@BotFather** and send `/newbot`.
2. Pick a name. BotFather gives you a **token** (looks like `123456:ABC...`). Save it.
3. Open your new bot and press **Start** (so it can message you).
4. Open **@userinfobot** and press Start. It shows your **Id** (a number). Save it.

## Step 2 — Make a GitHub repo
1. Go to github.com (sign up free if needed).
2. Tap **+ → New repository**. Name: `amazon-price-bot`.
3. Choose **Public** (free unlimited run time; your token stays secret).
   Private also works, but uses about 1,200 of your 2,000 free minutes a month.
4. Tick **Add a README file**. Tap **Create repository**.

## Step 3 — Add the 3 files
In the repo, tap **Add file → Create new file**. Paste the content, then **Commit**.

| File name to type | Content from |
|---|---|
| `bot.py` | bot.py |
| `requirements.txt` | requirements.txt |
| `.github/workflows/price-bot.yml` | price-bot.yml |

Type the third name exactly, with the slashes. GitHub makes the folders.

## Step 4 — Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- Name `TELEGRAM_BOT_TOKEN` → your token
- Name `TELEGRAM_CHAT_ID` → your Id number

## Step 5 — Allow the bot to save prices
**Settings → Actions → General → Workflow permissions** → choose **Read and write** → Save.

## Step 6 — Test it
**Actions** tab → **Amazon price watch** → **Run workflow** → tick **Send a test Telegram message** → Run.
You should get "✅ Amazon.eg price bot is working" in Telegram within a few minutes.

## How it works
- Runs every 3 hours, by itself.
- The first 2 days it only learns normal prices. Alerts start after that.
- Alert = price is 70% or more below the item's usual price.
- To change the %, edit `DROP_PERCENT: "70"` in the .yml file.
- If Amazon blocks most pages, you get one warning message per day.
