"""Instagram username/password login → Netscape cookies for yt-dlp.

Uses instagrapi (mobile-app private API / Bloks CAA login flow) instead of
scraping the web login endpoint. This matters because Instagram's anti-abuse
system evaluates a *device fingerprint* history, not just credentials — a
fresh fingerprint on every run looks like a new phone signing in from a
random IP every time, which is what a stateless web-login scrape does. A
persisted device+session (data/instagram_session.json) makes repeat logins
look like the same trusted phone returning, which is by far the single
biggest factor in avoiding checkpoints/challenges.

Even so, a login attempt that "looks risky" (datacenter IP, brand new
fingerprint, etc.) can get a bad_password/UserInvalidCredentials response
from Instagram EVEN WITH THE CORRECT PASSWORD — this is Instagram's risk
system, not necessarily wrong credentials. See INSTAGRAM_PROXY below.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.cookiejar import MozillaCookieJar, Cookie
from pathlib import Path
from typing import Callable

from bot import config as cfg
from bot.config import DATA_DIR, get_cookies_file

# INSTAGRAM_USERNAME/PASSWORD/PROXY/TOTP_SECRET are read as cfg.X (not
# imported by name) everywhere below — the admin panel can change them at
# runtime via reload_settings(), and `from bot.config import X` would freeze
# a stale copy in this module's namespace that never sees those updates.

# (username, choice) -> verification code. `choice` is instagrapi's
# CHOICE_EMAIL/CHOICE_SMS constant. Used to relay a code request out to
# whoever is driving an interactive login/signup (e.g. the admin via Telegram).
CodeProvider = Callable[[str, object], str]

logger = logging.getLogger(__name__)

INSTAGRAM_COOKIES_PATH = DATA_DIR / "instagram_cookies.txt"
INSTAGRAM_SESSION_PATH = DATA_DIR / "instagram_session.json"
_LOGIN_LOCK = threading.Lock()
_SESSION_MAX_AGE_SEC = 5 * 24 * 3600  # refresh every ~5 days
_LOGIN_FAILURE_COOLDOWN_SEC = 30 * 60  # don't hammer Instagram after a failed login
_last_login_failure_at = 0.0


def instagram_credentials_configured() -> bool:
    return bool(cfg.INSTAGRAM_USERNAME and cfg.INSTAGRAM_PASSWORD)


def ensure_instagram_cookies(*, force_refresh: bool = False) -> str | None:
    """
    Return a cookies file path usable by yt-dlp for Instagram.

    Priority:
    1. data/instagram_cookies.txt — either uploaded manually via the admin
       panel (📸 Instagram cookies), or written by auto-login below. Manually
       uploaded cookies never expire on their own (we can't refresh them
       ourselves) and are always preferred while they still look valid.
    2. Auto-login via INSTAGRAM_USERNAME/PASSWORD, if configured — refreshed
       every ~5 days or when the caller forces it.
    3. COOKIES_FILE / data/cookies.txt.
    """
    global _last_login_failure_at
    creds_ok = instagram_credentials_configured()

    with _LOGIN_LOCK:
        if not force_refresh and _cookies_look_valid(INSTAGRAM_COOKIES_PATH):
            if not creds_ok or _cookies_fresh(INSTAGRAM_COOKIES_PATH):
                return str(INSTAGRAM_COOKIES_PATH)

        if not creds_ok:
            if INSTAGRAM_COOKIES_PATH.is_file():
                return str(INSTAGRAM_COOKIES_PATH)
            return get_cookies_file()

        # A login just failed (e.g. bad credentials / checkpoint) — retrying on
        # every single request wastes a full HTTP round-trip per download and
        # visibly slows the bot down. Back off until the cooldown expires.
        if (
            not force_refresh
            and _last_login_failure_at
            and time.time() - _last_login_failure_at < _LOGIN_FAILURE_COOLDOWN_SEC
        ):
            if INSTAGRAM_COOKIES_PATH.is_file():
                return str(INSTAGRAM_COOKIES_PATH)
            return get_cookies_file()

        try:
            _login_and_save(INSTAGRAM_COOKIES_PATH)
            _last_login_failure_at = 0.0
            return str(INSTAGRAM_COOKIES_PATH)
        except Exception as exc:
            _last_login_failure_at = time.time()
            logger.error("Instagram auto-login failed: %s", _safe_err(exc))
            # Keep previous cookies if still present
            if INSTAGRAM_COOKIES_PATH.is_file():
                return str(INSTAGRAM_COOKIES_PATH)
            return get_cookies_file()


def instagram_cookies_status() -> dict:
    """Status for the admin panel: whether cookies exist, look valid, and age."""
    path = INSTAGRAM_COOKIES_PATH
    exists = path.is_file()
    valid = _cookies_look_valid(path) if exists else False
    age_days: float | None = None
    if exists:
        try:
            age_days = (time.time() - path.stat().st_mtime) / 86400
        except OSError:
            age_days = None
    return {
        "exists": exists,
        "valid": valid,
        "age_days": age_days,
        "auto_login_configured": instagram_credentials_configured(),
        "session_saved": INSTAGRAM_SESSION_PATH.is_file(),
        "username": cfg.INSTAGRAM_USERNAME,
        "proxy_configured": bool(cfg.INSTAGRAM_PROXY or cfg.YTDLP_PROXY),
        "totp_configured": bool(cfg.INSTAGRAM_TOTP_SECRET),
        "cooldown_remaining_sec": max(
            0.0, _LOGIN_FAILURE_COOLDOWN_SEC - (time.time() - _last_login_failure_at)
        )
        if _last_login_failure_at
        else 0.0,
    }


def save_uploaded_instagram_cookies(raw_bytes: bytes) -> None:
    """Save an admin-uploaded cookies.txt — raises ValueError if it doesn't look valid."""
    from bot.config import cookies_file_looks_valid

    tmp = INSTAGRAM_COOKIES_PATH.with_suffix(".tmp")
    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception as exc:
        raise ValueError(f"Could not read file as text: {exc}") from exc

    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    try:
        if not cookies_file_looks_valid(tmp):
            raise ValueError("Does not look like a Netscape cookies.txt file")
        if not _cookies_look_valid(tmp):
            raise ValueError("No 'sessionid' cookie found — export while logged into Instagram")
        tmp.replace(INSTAGRAM_COOKIES_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def refresh_instagram_cookies() -> str | None:
    """Force a new login (e.g. after empty-media / login-wall errors)."""
    return ensure_instagram_cookies(force_refresh=True)


def _cookies_fresh(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        return age < _SESSION_MAX_AGE_SEC
    except OSError:
        return False


def _cookies_look_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # sessionid is the critical Instagram auth cookie
    return bool(re.search(r"(^|\t)sessionid\t", text, re.M))


def _safe_err(exc: BaseException) -> str:
    """Never echo password material from exception text."""
    text = str(exc)
    password = cfg.INSTAGRAM_PASSWORD
    if password and password in text:
        text = text.replace(password, "***")
    return text[:400]


def _build_client():
    try:
        from instagrapi import Client
    except ImportError as exc:
        raise RuntimeError(
            "instagrapi is required for Instagram auto-login (pip install instagrapi)"
        ) from exc

    cl = Client()
    cl.delay_range = [1, 3]  # small human-like pause between requests

    # Reuse the saved device fingerprint + session so repeat logins look like
    # the same trusted phone returning, not a brand-new device every time —
    # this alone avoids most checkpoint/challenge triggers.
    if INSTAGRAM_SESSION_PATH.is_file():
        try:
            cl.load_settings(str(INSTAGRAM_SESSION_PATH), override_app_version=True)
        except Exception as exc:
            logger.warning(
                "Could not load saved Instagram session (%s) — starting fresh", exc
            )

    proxy = cfg.INSTAGRAM_PROXY or cfg.YTDLP_PROXY
    if proxy:
        cl.set_proxy(proxy)
    return cl


def _totp_code() -> str:
    if not cfg.INSTAGRAM_TOTP_SECRET:
        return ""
    try:
        import pyotp
    except ImportError as exc:
        raise RuntimeError(
            "pyotp is required for INSTAGRAM_TOTP_SECRET (pip install pyotp)"
        ) from exc
    return pyotp.TOTP(cfg.INSTAGRAM_TOTP_SECRET).now()


def _perform_login(cl, username: str, password: str, *, code_provider: CodeProvider | None) -> None:
    """Log `cl` in. With code_provider set, a 2FA/verification code, a
    checkpoint challenge, or Meta's age/"youth regulation" confirmation step
    is resolved interactively — the provider is asked live, and whatever it
    returns is submitted verbatim. Nothing is guessed or asserted on the
    account holder's behalf; each of these prompts only proceeds once a real
    person answers it, the same way tapping through them in the app would."""
    if code_provider is not None:
        cl.challenge_code_handler = code_provider
        _perform_login_interactive(cl, username, password, code_provider)
        return
    _perform_login_noninteractive(cl, username, password)


def _perform_login_noninteractive(cl, username: str, password: str) -> None:
    from instagrapi.exceptions import (
        BadPassword,
        ChallengeRequired,
        PleaseWaitFewMinutes,
        TwoFactorRequired,
    )

    verification_code = _totp_code()
    try:
        cl.login(username, password, verification_code=verification_code)
    except TwoFactorRequired as exc:
        raise RuntimeError(
            "Instagram requires a verification code (emailed, texted, or "
            "from an authenticator app). Set INSTAGRAM_TOTP_SECRET for "
            "automated authenticator-app 2FA, or use /admin → Instagram → "
            "🔐 Login now to enter an emailed/SMS code interactively."
        ) from exc
    except ChallengeRequired as exc:
        raise RuntimeError(
            "Instagram is asking for manual verification (an email/SMS code, "
            "an age/birthdate confirmation, or 'was this you?' confirmation) — "
            "this can't be fully automated. Use /admin → Instagram → "
            "🔐 Login now to resolve it interactively, log into this account "
            "from a real browser/phone once, or upload a cookies.txt instead."
        ) from exc
    except BadPassword as exc:
        raise RuntimeError(
            "Instagram rejected this login. This is usually NOT actually a "
            "wrong password — Instagram's risk system flags logins from "
            "server/datacenter IPs and fresh device fingerprints even with "
            "correct credentials. Set INSTAGRAM_PROXY to a residential/mobile "
            "proxy (most effective fix), or use /admin → Instagram cookies "
            "to upload a cookies.txt exported from a real browser session "
            "instead of automated login."
        ) from exc
    except PleaseWaitFewMinutes as exc:
        raise RuntimeError(
            "Instagram is rate-limiting login attempts on this account/IP — "
            "wait 15–30 minutes before it retries automatically."
        ) from exc


def _perform_login_interactive(cl, username: str, password: str, code_provider: CodeProvider) -> None:
    """Step through the CAA login manually (rather than cl.login()) so we can
    see — and interactively resolve — a "youth regulation" age-confirmation
    checkpoint before instagrapi's own fallback path turns it into a generic,
    unrecoverable "needs_upgrade" error. cl.login() doesn't expose this: on a
    non-2FA CAA failure it silently retries via login_legacy(), which hits
    the exact same checkpoint and fails the same way, surfacing only a
    misleading version-mismatch message with no way to intervene.
    """
    if not cl.bloks_caa_login_prepare(username=username):
        raise RuntimeError("Instagram did not return an account-access token for this login attempt.")

    result = cl.bloks_caa_login_send_request(password, username=username, auto_prepare=False)
    if cl.bloks_apply_login_response(result):
        return  # logged in on the first pass, nothing more to do

    if cl.bloks_caa_login_needs_two_step(result):
        code = _totp_code() or (
            code_provider(
                username, "the verification code Instagram just sent (email, SMS, or authenticator app)"
            )
            or ""
        ).strip()
        if not code:
            raise RuntimeError("No verification code was provided — login cancelled.")
        two_step = cl.bloks_caa_resolve_two_step_verification(result, verification_code=code)
        if two_step.get("logged_in"):
            return
        raise RuntimeError("Instagram rejected that verification code.")

    markers = cl._caa_result_action_markers(result)
    if any("YOUTH_REGULATION" in marker for marker in markers):
        _resolve_youth_regulation_checkpoint(cl, username, password, result, code_provider)
        return
    if any(marker.startswith("CAA_LOGIN_FALLBACK:") for marker in markers):
        raise RuntimeError(
            "Instagram routed this login to its legacy fallback path, which "
            "has been unreliable for this account today (the 'needs_upgrade' "
            "error from earlier) — likely a rate-limit from repeated attempts "
            "rather than something resolvable right now. Wait before retrying."
        )

    # Nothing we recognize — log the full response server-side (may contain
    # session-scoped tokens, so it's not echoed to the admin) and surface a
    # best-effort human-readable reason if the response has one.
    logger.warning("Unrecognized CAA login result for %s: %s", username, json.dumps(result, default=str)[:4000])
    reason = _bloks_best_effort_reason(result)
    detail = f" ({reason})" if reason else f" (markers: {markers})" if markers else ""
    raise RuntimeError(f"Instagram declined this login for an unrecognized reason{detail}. Check server logs for the raw response.")


def _bloks_best_effort_reason(result: dict) -> str:
    """Best-effort human-readable snippet from a Bloks result, for the parts
    of it that aren't a known action/marker — e.g. a plain title/message."""
    for key in ("title", "message", "reason", "error_type"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _resolve_youth_regulation_checkpoint(cl, username: str, password: str, result: dict, code_provider: CodeProvider) -> None:
    """Meta's age/"youth regulation" check on a first-time device. Ask the
    account holder to confirm their own birthdate live, in chat — never
    supplied or guessed by this code — then submit exactly what they say and
    retry the login once."""
    birthday = (
        code_provider(
            username,
            "your account's birthdate (DD-MM-YYYY) — Instagram is asking you to "
            "confirm it before this login can continue",
        )
        or ""
    ).strip()
    if not birthday:
        raise RuntimeError("No birthdate was provided — login cancelled.")

    extracted = cl._caa_extract_state(result)
    state = {
        "device_id": cl.android_device_id,
        "family_device_id": cl.phone_id,
        "qe_device_id": cl.uuid,
        "waterfall_id": cl.caa_waterfall_id,
        "machine_id": cl.mid,
        "flow_info": json.dumps({"flow_name": "new_to_family_ig_default", "flow_type": "ntf"}),
        "reg_info": extracted.get("reg_info", ""),
        "reg_context": extracted.get("reg_context", ""),
    }
    response = cl.caa_reg_graphql(
        "com.bloks.www.bloks.caa.reg.birthday.async",
        state=state,
        current_step=6,
        client_input_params={
            "accounts_list": [],
            "client_timezone": getattr(cl, "timezone_offset", 0),
            "birthday_or_current_date_string": birthday,
            "birthday_timestamp": int(time.time()),
            "os_age_range": "o18",
            "should_skip_youth_tos": False,
            "is_youth_regulation_flow_complete": False,
        },
        server_params={"si_device_param_network_info": ""},
    )
    if cl.bloks_apply_login_response(response):
        return

    # Birthday accepted but the flow didn't hand back a session directly —
    # retry the login request now that the checkpoint should be cleared.
    retry = cl.bloks_caa_login_send_request(password, username=username, auto_prepare=False, try_num=2)
    if cl.bloks_apply_login_response(retry):
        return
    raise RuntimeError("Instagram still didn't complete login after confirming the birthdate.")


def _save_session_and_cookies(cl, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    INSTAGRAM_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(str(INSTAGRAM_SESSION_PATH))
    _write_cookies_from_jar(path, cl.private.cookies)
    if not _cookies_look_valid(path):
        raise RuntimeError(
            "Instagram login succeeded but produced no sessionid cookie — unexpected."
        )
    logger.info("Instagram cookies saved to %s", path)


def _login_and_save(path: Path) -> None:
    """Non-interactive login used by the passive ensure_instagram_cookies() path
    (triggered by ordinary download requests) — must never block on a human."""
    username = cfg.INSTAGRAM_USERNAME or ""
    password = cfg.INSTAGRAM_PASSWORD or ""
    if not username or not password:
        raise RuntimeError("INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD not set")

    logger.info("Instagram auto-login as %s…", username)
    cl = _build_client()
    _perform_login(cl, username, password, code_provider=None)
    _save_session_and_cookies(cl, path)


def interactive_login(code_provider: CodeProvider) -> dict:
    """Admin-triggered login (e.g. /admin → Instagram → 🔐 Login now).

    Unlike the passive path, this resolves checkpoint challenges by asking
    code_provider(username, choice) for the code Instagram just sent —
    typically wired up to prompt the admin over Telegram and wait for a reply.
    """
    username = cfg.INSTAGRAM_USERNAME or ""
    password = cfg.INSTAGRAM_PASSWORD or ""
    if not username or not password:
        raise RuntimeError("INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD not set")

    global _last_login_failure_at
    with _LOGIN_LOCK:
        logger.info("Instagram interactive login as %s…", username)
        cl = _build_client()
        try:
            _perform_login(cl, username, password, code_provider=code_provider)
            _save_session_and_cookies(cl, INSTAGRAM_COOKIES_PATH)
        except Exception:
            _last_login_failure_at = time.time()
            raise
        _last_login_failure_at = 0.0
        return {"username": username}


def logout_instagram() -> None:
    """Best-effort remote logout (invalidates the session server-side), then
    always clears local session/cookie files regardless of whether that
    succeeded (e.g. no network, or nothing to invalidate)."""
    if INSTAGRAM_SESSION_PATH.is_file():
        try:
            cl = _build_client()
            if cl.user_id:
                cl.logout()
        except Exception as exc:
            logger.warning("Instagram remote logout failed (clearing local session anyway): %s", exc)

    INSTAGRAM_COOKIES_PATH.unlink(missing_ok=True)
    INSTAGRAM_SESSION_PATH.unlink(missing_ok=True)
    global _last_login_failure_at
    _last_login_failure_at = 0.0


def create_instagram_account(
    *,
    code_provider: CodeProvider,
    username: str,
    password: str,
    email: str,
    full_name: str = "",
) -> dict:
    """Create a new Instagram account via email verification.

    code_provider(username, choice) is asked for the email confirmation code
    Instagram sends — wire it up to prompt a human (e.g. the admin over
    Telegram) since we can't read an arbitrary inbox ourselves. Success isn't
    guaranteed: Instagram may still require a phone number or a captcha that
    this flow cannot solve, especially from a datacenter IP.
    """
    cl = _build_client()
    cl.challenge_code_handler = code_provider
    user = cl.signup_caa_email(username, password, email, full_name=full_name, attempts=6, wait_seconds=20)
    _save_session_and_cookies(cl, INSTAGRAM_COOKIES_PATH)
    return {"username": user.username, "user_id": str(user.pk)}


def _write_cookies_from_jar(path: Path, cookiejar) -> None:
    """Convert a requests/http.cookiejar CookieJar (instagrapi's session) to Netscape format."""
    jar = MozillaCookieJar()
    now = int(time.time())
    default_expiry = now + 90 * 24 * 3600
    for c in cookiejar:
        domain = c.domain or ".instagram.com"
        jar.set_cookie(
            Cookie(
                version=0,
                name=c.name,
                value=c.value,
                port=None,
                port_specified=False,
                domain=domain if domain.startswith(".") else f".{domain.lstrip('.')}",
                domain_specified=True,
                domain_initial_dot=True,
                path=c.path or "/",
                path_specified=True,
                secure=True,
                expires=c.expires or default_expiry,
                discard=False,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""},
                rfc2109=False,
            )
        )
    _write_mozilla_jar(path, jar)


def _write_mozilla_jar(path: Path, jar: MozillaCookieJar) -> None:
    tmp = path.with_suffix(".tmp")
    # MozillaCookieJar requires an existing file when constructed with a filename
    tmp.write_text("# Netscape HTTP Cookie File\n# https://curl.haxx.se/rfc/cookie_spec.html\n\n", encoding="utf-8")
    out = MozillaCookieJar(str(tmp))
    for cookie in jar:
        out.set_cookie(cookie)
    out.save(ignore_discard=True, ignore_expires=True)
    tmp.replace(path)
