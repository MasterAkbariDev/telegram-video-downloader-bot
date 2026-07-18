"""Bot configuration loaded from environment."""

import os
from pathlib import Path
import shutil

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60"))

QUALITY = os.getenv("QUALITY", "fast").strip().lower()
_MAX_HEIGHT = {"fast": 480, "balanced": 720, "best": 1080}
MAX_VIDEO_HEIGHT = _MAX_HEIGHT.get(QUALITY, 480)

# Soft size target for compression (MB). Videos larger than this are re-encoded
# even when 2 GB upload mode is on — keeps Instagram/reels snappy to send.
COMPRESS_TARGET_MB = float(os.getenv("COMPRESS_TARGET_MB", "25"))

# Cap libx264 threads so compression doesn't peg every CPU core (default 2).
FFMPEG_THREADS = max(1, int(os.getenv("FFMPEG_THREADS", "2")))

# Optional: Netscape cookies.txt for Instagram / login-required sites (see README)
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None
# Lower than 2s so Instagram feels snappy; raise if you hit 429s
# Polite delay between Instagram yt-dlp extracts (seconds)
INSTAGRAM_MIN_INTERVAL = float(os.getenv("INSTAGRAM_MIN_INTERVAL", "0.05"))

STANDARD_UPLOAD_LIMIT = 50 * 1024 * 1024
LARGE_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024

DOWNLOAD_DIR = ROOT_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

STATS_DB = DATA_DIR / "stats.db"
TELETHON_SESSION = DATA_DIR / "telethon_bot"


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))


def reload_settings() -> None:
    """Reload .env — call after admin updates credentials."""
    global BOT_TOKEN, TELEGRAM_PROXY, TELEGRAM_API_ID, TELEGRAM_API_HASH, ADMIN_IDS, QUALITY, MAX_VIDEO_HEIGHT
    global COOKIES_FILE, YTDLP_PROXY, INSTAGRAM_MIN_INTERVAL, COMPRESS_TARGET_MB

    load_dotenv(ROOT_DIR / ".env", override=True)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
    TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
    ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    QUALITY = os.getenv("QUALITY", "fast").strip().lower()
    MAX_VIDEO_HEIGHT = _MAX_HEIGHT.get(QUALITY, 480)
    COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None
    YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None
    INSTAGRAM_MIN_INTERVAL = float(os.getenv("INSTAGRAM_MIN_INTERVAL", "0.05"))
    COMPRESS_TARGET_MB = float(os.getenv("COMPRESS_TARGET_MB", "25"))


def get_cookies_file() -> str | None:
    """Resolved cookies path: explicit env, or data/cookies.txt if present."""
    if COOKIES_FILE:
        path = Path(COOKIES_FILE)
        if path.is_file():
            return str(path)
    default = DATA_DIR / "cookies.txt"
    if default.is_file():
        return str(default)
    return None


def large_upload_enabled() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_API_ID.isdigit())


def get_max_file_size() -> int:
    return LARGE_UPLOAD_LIMIT if large_upload_enabled() else STANDARD_UPLOAD_LIMIT


def get_max_filesize_label() -> str:
    return "2000M" if large_upload_enabled() else "50M"


def get_compress_target_bytes() -> int:
    """Target size after compression — independent of 2 GB upload mode."""
    soft = int(COMPRESS_TARGET_MB * 1024 * 1024)
    hard = get_max_file_size()
    # Never aim above the hard Telegram limit; default soft is 25 MB
    return min(soft, int(hard * 0.90))


def get_download_size_cap() -> int:
    """Allow downloading larger than upload limit when ffmpeg can compress afterward."""
    limit = get_max_file_size()
    if shutil.which("ffmpeg"):
        # Cap at 4× upload limit (e.g. 200 MB when limit is 50 MB)
        return min(limit * 4, max(limit, 200 * 1024 * 1024))
    return limit


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


MAX_FILE_SIZE = get_max_file_size()

URL_PATTERN = (
    r"https?://(?:www\.)?"
    r"(?:"
    r"youtube\.com|youtu\.be|"
    r"instagram\.com|instagr\.am|"
    r"spotify\.com|open\.spotify\.com|"
    r"tiktok\.com|vm\.tiktok\.com|"
    r"twitter\.com|x\.com|"
    r"facebook\.com|fb\.watch|"
    r"reddit\.com|"
    r"vimeo\.com|"
    r"soundcloud\.com|"
    r"twitch\.tv|"
    r"dailymotion\.com|"
    r"linkedin\.com|"
    r"pinterest\.com|"
    r"snapchat\.com|"
    r"rumble\.com|"
    r"bilibili\.com|"
    r"music\.apple\.com|"
    r"bandcamp\.com|"
    r"deezer\.com|"
    r"nicovideo\.jp|"
    r"streamable\.com|"
    r"drive\.google\.com|"
    r"dropbox\.com|"
    r"mediafire\.com|"
    r"archive\.org|"
    r"[\w.-]+\.[\w]{2,}"
    r")"
    r"[^\s<>\"']*"
)
