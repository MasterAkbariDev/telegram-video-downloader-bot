# Telegram Video Downloader Bot

**Repository:** [github.com/MasterAkbariDev/telegram-video-downloader-bot](https://github.com/MasterAkbariDev/telegram-video-downloader-bot)

A lightweight Telegram bot that downloads videos and music from YouTube, Instagram, Spotify, TikTok, and [hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) — then sends them directly in the chat.

Works in **private chats** and **groups**. No daily limits, no queues — paste a link and get your media.

## Features

- **One-command setup** — only your BotFather token is required
- **YouTube & Instagram** — full video download support
- **Spotify, SoundCloud, Apple Music** — audio extraction as MP3
- **600+ sites** via [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Group-friendly** — members can paste links without mentioning the bot
- **Progress updates** — live download status in chat
- **Fast mode** — when possible, Telegram fetches the video directly (no server download/upload)
- **Smart fallback** — downloads locally if direct send fails (YouTube, Spotify, etc.)

## Quick Start

### Prerequisites

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/download.html) (auto-installed on macOS/Linux when possible)

### Setup

```bash
git clone https://github.com/MasterAkbariDev/telegram-video-downloader-bot.git
cd telegram-video-downloader-bot
chmod +x setup.sh run.sh update.sh
./setup.sh
```

The setup script will ask for your **BotFather token** and handle everything else.

### Run

```bash
./run.sh
```

## Server Deployment (VPS / Linux)

Use this to run the bot 24/7 on a Ubuntu/Debian VPS (DigitalOcean, Hetzner, AWS, etc.).

### 1. Connect to your server

```bash
ssh root@YOUR_SERVER_IP
```

### 2. Install system dependencies

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git ffmpeg
```

### 3. Clone and set up the bot

```bash
cd /opt
git clone https://github.com/MasterAkbariDev/telegram-video-downloader-bot.git
cd telegram-video-downloader-bot
chmod +x setup.sh run.sh update.sh
./setup.sh
```

Paste your **BotFather token** when prompted.

### 4. Test it manually

```bash
./run.sh
```

Send a link to your bot on Telegram. If it works, press `Ctrl+C` and set up the service below.

### 5. Run as a background service (systemd)

```bash
chmod +x deploy/install-service.sh
./deploy/install-service.sh
```

This creates the service with the correct user and paths automatically.

Check status:

```bash
sudo systemctl status telegram-bot
```

View logs:

```bash
sudo journalctl -u telegram-bot -f
```

### 6. Update the bot later

```bash
./update.sh
```

This pulls the latest code, updates dependencies, and restarts the systemd service if it's running.

Or manually:

```bash
cd /opt/telegram-video-downloader-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegram-bot
```

### Server tips

| Topic | Recommendation |
|-------|----------------|
| **RAM** | 1 GB minimum (downloads are temporary) |
| **Disk** | 5–10 GB free (`downloads/` is auto-cleaned) |
| **Firewall** | No open ports needed — bot uses outbound HTTPS only |
| **Security** | Never share `.env`; keep token secret |
| **Groups** | Still disable privacy mode in @BotFather (`/setprivacy` → Disable) |

### YouTube / Instagram cookies (optional)

YouTube often blocks datacenter IPs. Instagram increasingly requires a
**logged-in session** for reels/posts.

**Option A — Instagram auto-login (fully automated)**

Set in `.env`:

```env
INSTAGRAM_USERNAME=your_username
INSTAGRAM_PASSWORD=your_password

# Strongly recommended — see "Why a proxy" below
INSTAGRAM_PROXY=http://user:pass@residential-proxy-host:port

# Only if the account has authenticator-app 2FA enabled
INSTAGRAM_TOTP_SECRET=your_totp_seed
```

The bot logs in using Instagram's **mobile app login flow** (via
[instagrapi](https://github.com/subzeroid/instagrapi)), not a scraped web
form — it persists a device fingerprint (`data/instagram_session.json`) so
repeat logins look like the same trusted phone returning instead of a new
device every time, which is what avoids most checkpoints. It saves
`data/instagram_cookies.txt` and refreshes automatically every ~5 days or
when Instagram rejects the current session, with a 30-minute cooldown after
a failed attempt so it doesn't hammer your account.

If the account has 2FA, `INSTAGRAM_TOTP_SECRET` generates codes automatically
(get the seed from Instagram: Settings → Two-factor authentication →
Authentication app → "Can't scan the QR code?"). **SMS-based 2FA can't be
automated** this way — use an authenticator app instead, or Option B.

**Why a proxy matters:** Instagram's risk system flags logins from
server/datacenter IPs — it can reject even the *correct* password from a VPS,
independent of any code fix. A residential/mobile proxy (`INSTAGRAM_PROXY`)
is the single biggest factor in reliable automated login; without one,
expect occasional rate-limits or rejected logins from cloud IP ranges. Use a
throwaway/dedicated Instagram account, not your personal one — there's a
real (if reduced) risk of a checkpoint or lock either way.

**Option B — Upload cookies via the admin panel (no password stored, but manual)**

Open the bot in Telegram as an admin → `/admin` → **📸 Instagram cookies** →
**⬆️ Upload cookies.txt**, then send a `cookies.txt` file exported from a
**real, logged-in browser session** (e.g. with the "Get cookies.txt LOCALLY"
extension, while logged into instagram.com). Nothing but session cookies
touches the server, and it avoids automated-login checks entirely — but
cookies aren't refreshed automatically, so you re-upload by hand when they
expire (you'll start seeing login-wall errors again).

Note: either option only unlocks a private account's posts if the logged-in
account actually **follows** it — no cookie or login trick bypasses that.

**Option C — Export cookies to a file manually**

```bash
# on your laptop
scp cookies.txt root@YOUR_VPS:/opt/telegram-video-downloader-bot/data/cookies.txt

# on the VPS
sudo chown "$(whoami):$(whoami)" /opt/telegram-video-downloader-bot/data/cookies.txt
sudo systemctl restart telegram-bot
```

Or set an explicit path in `.env`:

```env
COOKIES_FILE=/opt/telegram-video-downloader-bot/data/cookies.txt
```

Cookies expire; re-export if YouTube or Instagram starts failing again. Prefer exporting from a session that has used the same IP as the VPS when possible.

### YouTube PO Token provider (recommended — fixes HTTP 403 on most videos)

YouTube now requires a valid **PO (proof-of-origin) Token** for nearly every
format; without one, downloads fail with `HTTP Error 403: Forbidden` even
though the video extracts fine. `setup.sh` installs this automatically when
Docker is available. To set it up manually:

```bash
# 1. Deno (yt-dlp's recommended JS runtime — Node also works, but needs >=22)
curl -fsSL https://deno.land/install.sh | sh -s -- -y
ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno

# 2. PO Token provider (Docker)
docker run -d --name bgutil-provider -p 127.0.0.1:4416:4416 \
  --restart unless-stopped brainicism/bgutil-ytdlp-pot-provider
```

No further config needed — yt-dlp auto-detects the provider on `127.0.0.1:4416`
and the plugin ships in `requirements.txt` (`bgutil-ytdlp-pot-provider`).

### Alternative: run in `screen` or `tmux` (quick & simple)

If you don't want systemd:

```bash
apt install -y screen
screen -S tgbot
cd /opt/telegram-video-downloader-bot
./run.sh
# Detach: Ctrl+A then D
# Reattach later: screen -r tgbot
```

## Enable Group Support

By default, Telegram bots in groups only see messages that mention them. To let the bot respond to any pasted link:

1. Open [@BotFather](https://t.me/BotFather)
2. Send `/setprivacy` → select your bot → **Disable**
3. **Remove the bot from the group and add it again** (required after changing privacy)
4. In group settings → **Permissions** → allow the bot to **Send Messages**
5. Paste or share an Instagram/YouTube link — the bot now reads links from shared post previews too

## Enable Inline Mode

Use the bot as `@your_bot https://instagram.com/reel/…` in any chat (even ones the bot
can't message):

1. Open [@BotFather](https://t.me/BotFather) → `/setinline` → placeholder e.g. `Paste a video link…`
2. Open a private chat with the bot and tap `/start` once (needed to prepare the file)
3. In any chat type `@your_bot` + link — wait a few seconds (nothing shows yet)
4. Type the same `@your_bot` + link again → tap **📤 Send**

If you've downloaded that link before, **Send** appears immediately.

## Usage

| Action | How |
|--------|-----|
| Download a video | Send or paste any supported URL |
| Inline download | `@bot URL` (wait) → `@bot URL` again → **Send** |
| Get help | `/start` or `/help` |
| Multiple links | Send a message with several URLs — each is processed |

### Supported platforms (examples)

| Platform | Example |
|----------|---------|
| YouTube | `https://youtube.com/watch?v=…` |
| Instagram | `https://instagram.com/reel/…` |
| Spotify | `https://open.spotify.com/track/…` |
| TikTok | `https://tiktok.com/@user/video/…` |
| SoundCloud | `https://soundcloud.com/artist/track` |
| Twitter/X | `https://x.com/user/status/…` |

See the full list: [yt-dlp supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

## Project Structure

```
telegram-video-downloader-bot/
├── setup.sh          # One-time setup (token only)
├── update.sh         # Pull updates & restart service
├── run.sh            # Start the bot
├── check-telegram.sh # Test Telegram API connectivity
├── requirements.txt
├── .env.example
├── bot/
│   ├── __main__.py   # Entry point
│   ├── config.py     # Settings & URL patterns
│   ├── messages.py   # User-facing text & formatting
│   ├── downloader.py # yt-dlp download logic
│   └── handlers.py   # Telegram message handlers
└── downloads/        # Temporary files (auto-cleaned)
```

## Configuration

All configuration lives in `.env` (created by `setup.sh`):

```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_IDS=123456789,987654321

# Optional — 2 GB uploads (from https://my.telegram.org/apps)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash_here
```

### Admin panel

Admins (IDs in `ADMIN_IDS`) can open **⚙️ Admin panel** on `/start` or use `/admin`:

| Menu | Description |
|------|-------------|
| 📊 Statistics | Total downloads, unique users, data sent |
| 👥 Recent downloads | Activity log |
| 💾 Disk & storage | VPS disk usage + `downloads/` folder size |
| 🔑 2 GB upload API | Set API ID + Hash from [my.telegram.org](https://my.telegram.org/apps) |

**Upload limits:**

| Mode | Max file size | Requirements |
|------|---------------|--------------|
| Standard | 50 MB | Bot token only (default) |
| 2 GB | 2 GB | API ID + API Hash in admin settings |

With 2 GB mode, the bot uses [Telegram MTProto](https://core.telegram.org/api) (via Telethon) for large uploads — same credentials as described in [this guide](https://www.javathinking.com/blog/how-to-send-large-file-with-telegram-bot-api/).

## Limits

| Limit | Value | Notes |
|-------|-------|-------|
| Telegram upload (default) | 50 MB | Standard Bot API |
| Telegram upload (2 GB mode) | 2 GB | Requires API ID + Hash |
| Download quality | 480p default | Set `QUALITY=balanced` for 720p in `.env` |
| Rate limits | None | Bot-side; no artificial caps |

For files larger than 50 MB, the bot will notify you. Consider using a shorter clip or a lower-quality source.

## Publish to GitHub

```bash
git init
git add .
git commit -m "Initial commit: Telegram video downloader bot"
git branch -M main
git remote add origin https://github.com/MasterAkbariDev/telegram-video-downloader-bot.git
git push -u origin main
```

> **Important:** Never commit `.env` — it contains your bot token. It is already listed in `.gitignore`.

## Troubleshooting

**Service fails with `status=217/USER` or `Failed to determine user credentials`**
→ The service file still has `User=YOUR_USER` instead of a real Linux user. Reinstall the service:

```bash
chmod +x deploy/install-service.sh
./deploy/install-service.sh
```

**`TimedOut` / `telegram.error.TimedOut` on startup**
→ Your server cannot reach `api.telegram.org`. Test it:

```bash
chmod +x check-telegram.sh
./check-telegram.sh
```

Or manually:

```bash
curl -s --max-time 15 "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"
```

If that fails, Telegram is blocked on your server. Options:
1. Use a VPS outside the restricted region (e.g. EU/US)
2. Run a SOCKS5/HTTP proxy on the server and add to `.env`:

```env
TELEGRAM_PROXY=socks5://127.0.0.1:1080
```

Then reinstall deps and restart:

```bash
source .venv/bin/activate && pip install -r requirements.txt
./run.sh
```

**Bot doesn't respond in groups**
→ Privacy mode must be **Disabled** in @BotFather (`/setprivacy`). Then **remove and re-add** the bot to the group. Also check the bot has **Send Messages** permission. Shared Instagram posts hide the URL in a link preview — the latest version extracts those automatically.

**Inline mode doesn't appear**
→ Enable in @BotFather: `/setinline` → placeholder text.
  Open a private chat with the bot (`/start`) before using inline Prepare.

**"ffmpeg not found" errors**
→ Install ffmpeg: `brew install ffmpeg` (macOS) or `sudo apt install ffmpeg` (Ubuntu)

**"File is too large"**
→ Telegram limits bot uploads to 50 MB. Try a shorter video.

**Spotify download fails**
→ Some Spotify tracks require premium or are region-locked. yt-dlp may fall back to a YouTube match.

**Private Instagram/YouTube content**
→ Public Instagram reels increasingly need a logged-in session — upload
cookies via `/admin` → 📸 Instagram cookies (see above). Truly private posts
only work if the account behind those cookies follows the private account;
there's no way around that.

**YouTube fails with "HTTP Error 403: Forbidden"**
→ Set up the PO Token provider (see above) — YouTube now requires one for
nearly all formats.

## License

MIT — see [LICENSE](LICENSE)

## Disclaimer

This bot is for personal use. Respect copyright laws and the terms of service of the platforms you download from. The authors are not responsible for misuse.
