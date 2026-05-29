# Stats Bot

Discord bot for tracking scrim performance:

- matches played
- MVP count
- total kills
- kills per match (K/M)

## Commands

- `/record_match entries:<name:kills[:mvp] | name:kills[:mvp]> [note]`
- `/stats player name:<player name>`
- `/stats leaderboard sort_by:<kills|mvps|matches|km>`
- `/stats recent`
- `/add_player name:<player name>`

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in your Discord token.
4. Run `python src/main.py`.

## Entry format

Use the match recorder like this:

`Player One:12:mvp | Player Two:7 | Player Three:3:false`

The bot will create missing players automatically.

## Raspberry Pi deployment

1. Install Raspberry Pi OS Lite, then install `python3`, `python3-venv`, `python3-pip`, and `git`.
2. Clone this repo into a folder like `/opt/stats-bot`.
3. Create a virtual environment and install `requirements.txt`.
4. Create a `.env` file on the Pi with the token and optional guild ID.
5. Run the bot once manually to confirm it logs in.
6. Install a systemd service so the bot restarts on boot.

## systemd service example

```ini
[Unit]
Description=Stats Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/stats-bot
EnvironmentFile=/opt/stats-bot/.env
ExecStart=/opt/stats-bot/.venv/bin/python /opt/stats-bot/src/main.py
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

Enable it with:

```bash
sudo cp service/stats-bot.service /etc/systemd/system/stats-bot.service
sudo systemctl daemon-reload
sudo systemctl enable stats-bot
sudo systemctl start stats-bot
sudo systemctl status stats-bot
```
