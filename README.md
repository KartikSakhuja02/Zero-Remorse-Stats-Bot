# Stats Bot

Discord bot for tracking scrim performance and profile registration:

- player_name
- matches
- MVP count
- total kills
- kill per match (K/M)

## Commands

- `/record_match entries:<name:kills[:mvp] | name:kills[:mvp]> [note]`
- `/stats player name:<player name>`
- `/stats leaderboard sort_by:<kills|mvps|matches|km>`
- `/stats recent`
- `/add_player name:<player name>`
- `/profile_setup`
- `/profile member:<discord member>`

This version stores one aggregate row per player. Each recorded match updates that player's totals.
The profile workflow watches a submission channel, OCRs screenshots with OpenRouter, saves the image on the Pi, and DMs the user a private approval prompt.
`/profile` builds a persistent profile card in the stats channel, attaches the submitted screenshot, and can pull live profile stats from Tracker.gg when you set `TRACKER_API_KEY`, `TRACKER_TITLE_SLUG`, and `TRACKER_PLATFORM`.

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in your Discord token, submission channel IDs, OpenRouter key, optional Tracker.gg settings, screenshot directory, and PostgreSQL URL.
4. Run `python src/main.py`.

## Entry format

Use the match recorder like this:

`Player One:12:mvp | Player Two:7 | Player Three:3:false`

The bot will create missing players automatically.

## Raspberry Pi deployment

1. Install Raspberry Pi OS Lite, then install `python3`, `python3-venv`, `python3-pip`, `git`, and PostgreSQL.
2. Create the PostgreSQL database and user on the Pi.
3. Clone this repo into a folder like `/opt/stats-bot`.
4. Create a virtual environment and install `requirements.txt`.
5. Create a `.env` file on the Pi with the token, optional guild ID, submission channel ID, stats channel ID, OpenRouter key, optional Tracker.gg settings, screenshot directory, and `DATABASE_URL`.
6. Enable the Discord bot's Message Content Intent in the Developer Portal so it can watch image posts.
7. Run the bot once manually to confirm it logs in and creates tables.
8. Install a systemd service so the bot restarts on boot.

## PostgreSQL setup on the Pi

Use a local PostgreSQL database if the Pi is hosting the data itself:

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl enable postgresql
sudo systemctl start postgresql
sudo -u postgres psql
```

Inside `psql`, create the bot user and database:

```sql
CREATE USER stats_bot WITH PASSWORD 'change-this-password';
CREATE DATABASE stats_bot OWNER stats_bot;
GRANT ALL PRIVILEGES ON DATABASE stats_bot TO stats_bot;
\q
```

Then set the connection string in `.env`:

```env
DATABASE_URL=postgresql://stats_bot:change-this-password@localhost:5432/stats_bot
```

If you already have a remote PostgreSQL server, use its host instead of `localhost`.

## Profile registration flow

1. Create a text channel named `submit-your-profile` and put its channel ID in `PROFILE_SUBMISSION_CHANNEL_ID`.
2. Create a text channel for profile cards and put its channel ID in `PROFILE_STATS_CHANNEL_ID`.
3. Run `/profile_setup` once to post the instructions in the submission channel.
4. Players send a screenshot of their profile in that channel.
5. The bot OCRs the screenshot with OpenRouter, saves the image on the Pi, extracts the player name, and sends the user a DM with Approve and Decline buttons.
6. Approving saves the profile row with zeroed stats and posts the profile card in the stats channel.
7. `/profile member:<discord member>` refreshes that player's card in the stats channel.
8. Scrim result submissions refresh the saved profile cards automatically.
9. If the bot cannot delete the original screenshot message, check that it has `Manage Messages` in the submission channel.

Note: Discord does not support truly private replies for normal message listeners, so the confirmation step is handled by DM.

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
