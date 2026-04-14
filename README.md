# Bot RDV Préfecture

Telegram bot that monitors [rdv.anct.gouv.fr](https://rdv.anct.gouv.fr) and sends an instant alert when an appointment slot becomes available for renewing a récépissé.

## Features

- Checks for available slots every 60 seconds
- Sends a Telegram alert the moment slots appear
- Handles blocks, rate-limits, and CAPTCHAs with automatic backoff
- Auto-starts monitoring on launch

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/anesabdelli/bot-rdv-prefecture.git
cd bot-rdv-prefecture
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

- **BOT_TOKEN** — get it from [@BotFather](https://t.me/BotFather) on Telegram
- **CHAT_ID** — send `/start` to your bot, it will reply with your chat ID

### 4. Run the bot

```bash
python bot.py
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help and your chat ID |
| `/monitor` | Start monitoring |
| `/stop` | Stop monitoring |
| `/check` | Run a one-time check right now |
| `/status` | Show current bot status |

## Hosting (free, 24/7)

See the deployment steps below to host on Railway for free.

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables in the Railway dashboard
4. Done — the bot runs 24/7
