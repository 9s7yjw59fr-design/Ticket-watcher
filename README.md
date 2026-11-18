# Theatre ticket watcher (Linnateater + Draamateater)

Runs in the cloud on GitHub Actions and sends alerts to your Telegram.
Nothing runs on your laptop.

## Rhythm
- Night: off (about 00:00-08:00)
- Daytime: every 10 min for returned tickets
- Last 48 h before a watched show: every 5 min
- Sale openings: phone alert 20 min before, then every 30 s for 30 min

## Setup (once, about 20 minutes)
1. Telegram bot (5 minutes):
   - In Telegram, open @BotFather, send /newbot, pick a name.
     It replies with a token like 123456:ABC-xyz. Keep it private.
   - Open your new bot and send it any message, e.g. "hi".
   - In a browser open https://api.telegram.org/bot<TOKEN>/getUpdates
     (your token in place of <TOKEN>) and find "chat":{"id": 123456789}.
     That number is your chat ID.
2. Create a GitHub repository and upload these files.
3. Repository > Settings > Secrets and variables > Actions > New secret:
   - TELEGRAM_BOT_TOKEN (the token from step 1)
   - TELEGRAM_CHAT_ID (the number from step 1)
   - PILETILEVI_USER, PILETILEVI_PASS (Linnateater)
   - PILETIMAAILM_USER, PILETIMAAILM_PASS (Draamateater)
   Passwords live only here, encrypted. Never in config.yaml or in chat.
4. Actions > ticket-watch > Run workflow > mode: test. Your bot should message you.
5. Edit config.yaml: productions, event links, show dates, sale openings.

## Calibration (before switching on the basket)
The button selectors in config.yaml are first guesses.
1. Run workflow with mode: calibrate
2. Download the "debug" artifact (screenshots + HTML of each page) and share it.
3. Selectors get fixed, run again.
4. When alerts and basket steps work, set add_to_basket: true.

## Cost
A public repository runs free (secrets stay hidden, only the watchlist is
visible). A private one exceeds the free 2,000 minutes at this rhythm; then
use GitHub Pro or change the cron to every 10 min.
