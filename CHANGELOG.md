# Changelog

All notable changes to this bot are documented here.

## 1.8.29 — 2026-09-22

### Added
- **Occasional like/save on Instagram posts the bot fetches, using our own
  logged-in session.** An account that only ever silently fetches post data
  through the API and never does anything a real person does (like, save,
  browse) is itself an unusual, bot-shaped traffic pattern, independent of
  anything about the login flow. `maybe_humanize_instagram_activity()` in
  `bot/instagram_auth.py` now fires (fire-and-forget, in a background
  thread, with a 2–9s human-like delay) whenever an Instagram URL is
  resolved: likes ~55% of the time, saves ~7% of the time — "often but not
  always" for likes, "a lot less" for saves, deliberately not a fixed
  rate. No-op when there's no working login session (nothing to act as).
  Verified live: `media_pk_from_url()` + `media_like()` + `media_save()`
  against a real post, both returned success.

## 1.8.28 — 2026-09-22

### Fixed
- **`cl.bloks_extract_context_data()` returned "" for the two-step
  verification `code_entry` step, on every account tested** — captured and
  confirmed live against both `dpdotell` and the main configured account.
  instagrapi's extractor pairs the first `(dkc keys)`/`(dkc values)` group
  it finds right after an action's `(f4i ...)` wrapper, but on this
  entrypoint response Instagram nests `context_data` one level deeper —
  inside the *value* of an outer `server_params`/`client_input_params`
  pair — and the extractor's cursor jumps straight past that nested group
  without ever looking inside it, so it always returned "" here even
  though the real token is present in the raw response. Added
  `_bloks_deep_context_data()`, which searches directly for the
  `context_data` key/value pair at any nesting depth (verified byte-for-byte
  against the actual captured response) and now backs all three steps of
  `_resolve_caa_two_step_verification()` (1.8.26).

## 1.8.27 — 2026-09-22

### Fixed
- **Every two-step failure was misreported as "Instagram rejected that
  verification code," even when no code was ever submitted.** The check
  that was supposed to tell "didn't reach the code step" apart from "code
  was wrong" tested for the substring `"code"` in the failure reason — but
  every one of the "didn't reach it" reasons (`missing code_entry
  context_data`, `missing code_entry_async context_data`) contains "code"
  as part of a sub-step's own name, so the check always matched and always
  blamed the code. Fixed: any non-empty reason now means "didn't reach
  submission" (verified against both instagrapi's own resolver and
  `_resolve_caa_two_step_verification` added in 1.8.26 — a wrong/expired
  code always comes back with an *empty* reason instead, since submission
  did happen in that case).
- Logs the raw entrypoint response when Instagram doesn't offer a
  code-entry step at all — this can mean the account is being routed to a
  non-code verification method (e.g. in-app device approval) that isn't
  modeled yet, which needs the raw response to diagnose further.

## 1.8.26 — 2026-09-22

### Fixed
- **The CAA two-step verification code was requested from the admin BEFORE
  Instagram had actually been told to send one.** `instagrapi`'s
  `bloks_caa_resolve_two_step_verification()` bundles three network calls
  into one: `entrypoint` → `code_entry` → `submit_code`. Instagram only
  dispatches the code as a side effect of the `code_entry` step — but that
  whole method takes the code as an input parameter, so our code had to ask
  for the code *before* calling it at all, i.e. before any of those three
  steps had run. Every "check your email/SMS for the code" prompt was
  therefore sent to the admin before Instagram had any code to send,
  which is exactly why checking immediately kept turning up nothing. Added
  `_resolve_caa_two_step_verification()`, which runs `entrypoint` and
  `code_entry` first (triggering the actual send) and only then asks for
  the code, before submitting it. TOTP (authenticator app) codes are
  unaffected — those don't need a "send" step, since the code already
  exists locally.

## 1.8.25 — 2026-09-22

### Fixed
- **Albums/carousels of more than 10 items were silently truncated.**
  Telegram allows at most 10 items per `sendMediaGroup` call, and every
  place we built a media group sliced with `[:10]` instead of splitting
  the rest into follow-up messages — so anything past the 10th photo/video
  in a post just vanished. Instagram allows up to 20 items per carousel, so
  this was a real, regular loss. `bot/uploader.py`'s `_send_album()` now
  sends any number of items across as many `reply_media_group` calls as
  needed (10 at a time), including the CDN-hotlink-failed → re-download
  fallback path; the cached-album re-send path in `bot/handlers.py` and the
  extraction-time caps in `bot/instagram.py`, `bot/twitter.py`, and
  `bot/hikerapi.py` no longer discard anything past the 10th item either.
- **Interactive Instagram login (admin "🔐 Login now", and the same path
  used right after account creation) always performed a brand-new
  password-based CAA login, even when a previously saved session was
  already loaded and still valid.** `cl.login()` itself checks this first
  (`account_info()` on the existing session, only falling back to a fresh
  CAA login if that fails) — but the manual Bloks orchestration built to
  handle the youth-regulation checkpoint bypassed `cl.login()` entirely and
  so skipped that shortcut on every single call, including retries. Every
  retry was therefore indistinguishable from a fresh suspicious login
  attempt to Instagram's abuse detection, which compounds exactly the
  checkpoint/challenge problem it was trying to work around. Now mirrors
  `cl.login()`'s own check before doing anything else.
- **No cooldown on manual login retries.** Added a 45s minimum gap between
  "Login now" attempts — rapid re-taps (including while debugging) are
  themselves a signal Instagram's fraud system watches for.

## 1.8.24 — 2026-09-21

### Fixed
- **Interactive login had no handling for `ChallengeRequired`/`BadPassword`/
  `PleaseWaitFewMinutes`/`TwoFactorRequired` raised directly by the
  lower-level CAA calls.** Moving to manual orchestration (1.8.18, for the
  age checkpoint) bypassed `cl.login()`'s own exception handling without
  replacing it — a challenge raised at the very first preflight call
  (`bloks_caa_login_prepare`) surfaced as a bare, unhandled `challenge_required`
  instead of being resolved interactively the way it already was before that
  refactor. Restored: `ChallengeRequired` now resolves via `challenge_resolve()`
  same as before, and the other three get the same clear messages the
  non-interactive path already had.

## 1.8.23 — 2026-09-21

### Added
- **Signup now actually establishes a session.** `instagrapi`'s
  `signup_caa_email()` only extracts the newly-created user's metadata — it
  never calls the same session-establishing step a login does, so account
  creation succeeded but produced no usable cookies. `create_instagram_account()`
  now logs in immediately with the just-created credentials, reusing the same
  already-warmed client from signup.

### Fixed
- **Two-step verification blamed the code for failures that happened before
  the code was ever submitted.** `bloks_caa_resolve_two_step_verification()`
  walks three sub-steps (entrypoint → code entry → submit) and returns a
  `reason` describing exactly which one failed — e.g. `"missing code_entry
  context_data"` when it never got far enough to submit anything. This was
  being discarded, and any non-`logged_in` result was reported as "Instagram
  rejected that verification code" regardless of whether a code was even
  tried. Now surfaces the real reason and only blames the code when the
  failure actually happened at that step.

## 1.8.22 — 2026-09-21

### Fixed
- Birthdate confirmation reached the API and got `200`, but login still
  didn't complete on retry. Two gaps closed:
  - The prompt asked for `DD-MM-YYYY` but accepted whatever was typed
    verbatim — a reply like `27/09/2003` (slashes) may have been silently
    mis-parsed by Instagram's field. Now parses several common formats
    (`DD-MM-YYYY`, `DD/MM/YYYY`, `YYYY-MM-DD`, `MM/DD/YYYY`, ...) and
    normalizes to the one this endpoint was captured accepting.
  - The birthday-submission response and the login retry that follows it
    weren't inspected on failure — just a generic "still didn't complete"
    message. Both are now logged server-side (markers + raw response) so a
    repeat failure is diagnosable from one log pull instead of another
    live round-trip.

### Fixed
- **The age-checkpoint handler from 1.8.18 never actually triggered.**
  `instagrapi`'s `_caa_result_action_markers()` expects the *wrapped*
  `{"result": ...}` shape `bloks_caa_login()` returns, not the raw
  `bloks_caa_login_send_request()` payload used directly — passing it
  unwrapped silently returned `[]` every time, regardless of what Instagram
  actually sent. Confirmed from production logs: the same
  `BloksCAARegUnderAgeBlockingController` checkpoint was present both times
  reported as "unrecognized reason" — detection was the bug, not Instagram's
  response. Fixed by wrapping the result before extracting markers.

### Changed
- **Randomized device fingerprint per account.** `instagrapi`'s bundled
  default device profile (a Pixel 8 Pro on one exact Android build) is
  shared by every user of the library who doesn't override it — a
  distinctive, mass-produced signature that's an easy pattern for
  Instagram's fraud detection to key on, independent of anything else about
  a request. Now picks from a small pool of realistic device profiles,
  deterministically per account (same account keeps the same device across
  restarts — consistency matters for trust — but different accounts no
  longer share an identical fingerprint). Applies to new logins/signups
  only; an account with an existing saved session keeps its device.

## 1.8.19 — 2026-09-21

### Fixed
- Interactive login's "unrecognized reason" failure gave no detail to act
  on. Now logs the full raw CAA response server-side (not echoed to the
  admin, since it can carry session-scoped tokens) and surfaces a
  best-effort human-readable reason when the response has one, instead of
  just an empty marker list.

## 1.8.18 — 2026-09-21

### Added
- **🔐 Login now handles Meta's age/"youth regulation" checkpoint.** Root
  cause of the persistent `needs_upgrade` ("Your version of Instagram is
  out of date") error identified precisely: it's not a version or account
  issue — it's Meta's age-verification flow, triggered for any first-time
  ("cold start") automated device, that `instagrapi`'s `cl.login()` has no
  handling for and silently mishandles into that generic error. The
  interactive login now steps through the CAA flow manually so it can catch
  this checkpoint and — same as it already does for 2FA codes — ask the
  admin, live in chat, to confirm the account's real birthdate, then submits
  exactly what they answer and resumes the login. Nothing is guessed or
  submitted without a person directly confirming it in the moment. Needs a
  live `🔐 Login now` run to complete once per device; after that the device
  should no longer register as a first-time login.

## 1.8.17 — 2026-09-21

### Added
- **`HIKERAPI_KEY` — managed-API fallback for login-walled Instagram posts.**
  Self-hosted Instagram auto-login runs one account from one server IP,
  which Instagram's risk system is built to catch regardless of which
  library drives it — that's what all of today's testing kept confirming.
  Production Instagram tools solve this by not self-hosting login at all:
  they call a managed API (in this case [HikerAPI](https://hikerapi.com),
  built by the `instagrapi` maintainers) that runs its own residential-proxy
  and account pool server-side. Wired in as a **last resort**, tried only
  after free extraction (yt-dlp + anonymous scraping) has already failed —
  normal downloads still cost nothing; a login-walled post costs about
  $0.0006. Scales to any number of users without touching a shared cookie
  per request. 100 free requests to try it, no card required — see README.

## 1.8.16 — 2026-09-21

### Fixed
- **2FA code retry failed with "Your version of Instagram is out of date."**
  Submitting the verification code called `cl.login()` a second time, which
  replays the *entire* device-attestation/Bloks CAA sequence from scratch —
  a known trigger (open upstream: subzeroid/instagrapi#2807) for Instagram
  misreporting a needs_upgrade rejection on accounts that require a
  verification code. Now the code is submitted against the *same* Bloks
  challenge the first attempt already opened (instagrapi keeps that context
  on the exception it raises), instead of restarting the whole login.

## 1.8.14 — 2026-09-21

### Fixed
- **Admin panel showed stale Instagram/API status after saving.** Several
  modules (`bot/instagram_auth.py`, `bot/admin.py`) imported mutable config
  values (`INSTAGRAM_USERNAME`, `INSTAGRAM_PASSWORD`, `INSTAGRAM_PROXY`,
  `INSTAGRAM_TOTP_SECRET`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) with
  `from bot.config import X`, which freezes a copy at import time — a classic
  Python gotcha. Saving new credentials via the admin panel (or editing
  `.env` directly) updated `bot.config`'s copy but never reached these
  frozen copies, so e.g. **🔐 Login now stayed hidden after setting
  username/password** until a full bot restart. Fixed by reading these
  live off the `bot.config` module everywhere instead. Also added a
  `reload_settings()` call whenever the admin panel is opened, so `.env`
  edits made outside the bot (SSH) are picked up without a restart.
- **`/cancel` said "Nothing in progress" even with a prompt pending.** It
  only recognized 2 of the 11 "waiting for a reply" states (only the 2 GB
  API ID/Hash flow) — none of the new Instagram username/password/proxy/TOTP/
  signup prompts, or an interactive login/signup waiting on a verification
  code. `/cancel` now recognizes all of them from one shared list.

## 1.8.15 — 2026-09-21

### Fixed
- **🔐 Login now didn't ask for a 2FA/verification code.** Instagram can send
  an emailed/texted code (not just resolve a checkpoint) during login — that
  path only raised a static "set INSTAGRAM_TOTP_SECRET" error instead of
  prompting interactively like the checkpoint-challenge path already did.
  Now both are resolved the same way: the bot asks for the code in chat and
  retries the login with it.

## 1.8.13 — 2026-09-21

### Fixed
- **Critical:** a malformed `cookies.txt` (empty, wrong format, HTML error
  page, etc.) crashed *every* download that touched cookies — including all
  Instagram links — with a raw `does not look like a Netscape format cookies
  file` error. Cookie files are now validated before use; an invalid one is
  ignored (falls back to cookie-less / auto-login) instead of crashing.
- **YouTube downloads failing with HTTP 403** on most/all links. YouTube now
  requires a valid PO (proof-of-origin) Token for nearly every format, and the
  bundled JS-challenge solver needs Node **22+** (many servers ship 18, which
  silently failed). Fixed by preferring **Deno** as the JS runtime and adding
  support for a **bgutil PO Token provider** (see README) — both verified
  fixing real 403s end-to-end.
- Instagram auto-login retried on **every single request** when credentials
  were invalid, adding a failed login round-trip to every download. Now backs
  off for 30 minutes after a failure instead of retrying every time.
- **Instagram auto-login rewritten on `instagrapi`** (mobile app / Bloks
  login flow) instead of a scraped web-login form. Persists a device
  fingerprint + session (`data/instagram_session.json`) across restarts so
  repeat logins look like the same trusted phone, not a new device every
  time — this is what actually avoids most checkpoints. Verified this gets
  meaningfully further than the old flow (through the full device-attestation
  login sequence) instead of a generic rejected-credentials response.
- Added automated 2FA support via `INSTAGRAM_TOTP_SECRET` (authenticator-app
  codes generated with `pyotp` — no human needs to type a code) and a
  dedicated `INSTAGRAM_PROXY` option — server/datacenter IPs are what
  Instagram's risk system flags hardest, sometimes rejecting even correct
  credentials, so a residential/mobile proxy is the biggest lever for
  reliable automated login.
- ffmpeg compression had no concurrency limit — several videos compressing at
  once (e.g. a busy group chat) could spawn unbounded ffmpeg processes and
  drive CPU to 100%. Compression is now capped to fit available cores, and
  runs at lower CPU/IO priority (`nice`/`ionice`) so it doesn't starve the bot.

### Added
- **Admin panel → 📸 Instagram**, fully rebuilt:
  - **🔐 Login now** — triggers a login on demand and resolves checkpoint
    challenges *interactively*: if Instagram asks for a verification code,
    the bot asks the admin for it in chat and feeds it back into the login.
  - **🚪 Logout** — invalidates the session with Instagram (when possible)
    and clears the local session/cookies.
  - **✏️ Username / ✏️ Password / 🌐 Proxy / 🔑 TOTP secret** — edit auto-login
    settings from the bot instead of SSH + `.env`.
  - **➕ Create account** — a guided, admin-driven signup wizard (username →
    password → email → name) using Instagram's email-verification signup
    flow; the bot relays the emailed code request into chat. Not guaranteed
    to succeed — Instagram may still demand a phone number or a captcha this
    can't solve, especially from a server IP — but it's there.
  - **⬆️ Upload cookies.txt** (unchanged): a no-password alternative to
    auto-login, exported from a real logged-in browser session.

## 1.8.12 — 2026-07-19

### Added
- **Spotify track links** are accepted in chat and inline. Metadata reads
  title/artist from Spotify embed when oEmbed omits the artist.

### Changed
- Music matching (Spotify / unavailable YouTube) searches **SoundCloud and
  Audius in parallel**, then downloads the best match (YouTube search last)
- Dropped public **Piped / Invidious / Mixcloud** music sources — instances
  return 401/403 or streams with no usable audio
- User status shows a single **Finding track…** step (no source-hunting stages)
- Instagram: browser TLS impersonation enabled; login-wall / empty-media errors
  now tell you to add Instagram cookies instead of dumping yt-dlp text
- Instagram **auto-login** via `INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD` in
  `.env` (saves `data/instagram_cookies.txt`, refreshes on auth failures)

## 1.8.11 — 2026-07-19

### Changed
- **Inline mode redesign** — works in chats the bot can't message:
  - First `@bot URL` prepares in the background (waits briefly for fast links)
  - When ready, only **📤 Send** is shown (Telegram `file_id` cache)
  - Repeat of the same link is instant via cache (no re-download / no status stages)
  - Same URL is never prepared twice while in-flight; carousels use the first item
  - No Preparing placeholder and no ready DMs
  - Slow VPS: answers before Telegram’s query expires; failed prepares retry on
    the next search instead of staying empty
- Cached links pasted in chat skip Extracting/Uploading status — media is
  re-sent from the stored Telegram `file_id` immediately (including photo albums)

## 1.8.7 — 2026-07-18

### Fixed
- Pinterest video pins kept sending the cached poster photo after the path fix;
  stale image cache entries are dropped when the pin has an MP4
- Download progress total for HLS (e.g. Pornhub) no longer jumps around — uses a
  stable bitrate×duration estimate instead of fluctuating fragment guesses

### Changed
- ffmpeg compression capped to 2 threads by default (`FFMPEG_THREADS`)
- Compression status shows a real progress bar (time encoded / duration)

## 1.8.6 — 2026-07-18

### Fixed
- Pinterest video pins (new `videos/iht/expMp4/…` CDN paths) were sent as photos
  because only the poster image matched; video URLs are detected again

## 1.8.5 — 2026-07-18

### Added
- Quality picker for long YouTube, X, and adult videos: thumbnail + title +
  height buttons (360/480/720/1080) + Cancel before download
- Skips picker for Shorts, Instagram, Pinterest, TikTok, audio, and single-height videos

## 1.8.4 — 2026-07-18

### Added
- Pinterest pins (`pin.it` / `pinterest.com`) — images **and** videos via pinimg CDN
  (Telegram fetches them; no VPS download)
- Admin panel: **Failures & requests** log (unsupported sites users tried + broken
  supported downloads) with top hosts and clear button
- Welcome / help / about list Pinterest with the other supported platforms

## 1.8.3 — 2026-07-18

### Added
- Adult video hosts in the supported URL allowlist (Pornhub, XVideos, xHamster,
  RedTube, XNXX, SpankBang, Eporner, YouPorn, Tube8, Beeg / beeg.site / beeg.team)
- Welcome / help / about mention adult sites

## 1.8.2 — 2026-07-18

### Fixed
- YouTube Music / Topic tracks that return “Video unavailable” now fall back to a
  matching upload (YouTube search → mirrors → SoundCloud) using oEmbed title/artist
- YouTube Music links download as audio
- Enable Node as yt-dlp JS runtime when available (sig/n challenges)

## 1.8.1 — 2026-07-18

### Changed
- YouTube: prefer progressive MP4 (no ffmpeg merge) for faster Shorts / watch downloads
- YouTube Music links (`music.youtube.com`) normalize to `www.youtube.com` before extract
- Clearer error when a YouTube video is unavailable

### Notes
- YouTube CDN hotlink (like Instagram/X) is not possible: `googlevideo.com` URLs are
  IP-bound, so Telegram’s servers get “Failed to get http url content”

## 1.8.0 — 2026-07-18

### Added
- X (Twitter) photos, albums, and videos via syndication CDN (Telegram hotlink when possible)
- Private DMs reply with an “unsupported link” message for unknown sites; groups stay silent

### Changed
- Welcome / help / about list X alongside YouTube, Instagram, TikTok, SoundCloud

## 1.7.2 — 2026-07-18

### Changed
- Instagram photo albums send via CDN URLs (Telegram fetches them) — much faster, no VPS download
- Parallel slide downloads + skip HEAD probes when a disk fallback is needed
- Faster Instagram scrape (fewer fallback pages) and lighter yt-dlp delays for reels

## 1.7.1 — 2026-07-18

### Added
- TikTok support (`tiktok.com`, `vt.` / `vm.` short links) with `curl_cffi` TLS impersonation
- Media captions restored: title, uploader, platform, size, and link to the original post
- Captions on photos and the first album item

### Fixed
- TikTok “Access denied” / 403 (same-session download + Chrome impersonation)
- Instagram multi-photo albums failing with “media not found” / `file://` upload bugs
- Media always sent as a reply to the user’s message

### Changed
- Welcome / help / about list YouTube, Instagram, TikTok, and SoundCloud

## 1.6.9 — 2026-07-18

### Changed
- Cleaner welcome / help / about copy (YouTube, Instagram, SoundCloud only)
- Removed inline-mode section from user messages
- Update screens say “update” instead of `update.sh`

## 1.6.8 — 2026-07-18

### Fixed
- Update check now uses `git fetch` + HTTP mirrors so latest GitHub version is detected reliably

### Changed
- Removed separate **Check for updates** button — **Update bot** checks GitHub first
- Background update check every hour; admins still get **one DM per new version**

## 1.6.7 — 2026-07-18

### Fixed
- Instagram multi-photo carousels failing with “video could not be found”
- Scraper no longer bails when the page contains unrelated `video_url` blobs
- Stronger parsing for `xdt_shortcode_media` / sidecar / `carousel_media`

## 1.6.6 — 2026-07-18

### Added
- Changelog file and admin panel **Changelog** view
- Startup update check: admins get a **one-time** DM when a newer version is on GitHub
- Admin **Check for updates** button (manual)

### Changed
- Chat messages only trigger on **plain** Instagram / YouTube / SoundCloud links
- Text-bound hyperlinks (`TEXT_LINK`) are ignored

## 1.6.5 — 2026-07-18

### Changed
- Improved plain-URL extraction for Instagram, YouTube, and SoundCloud
- UTF-16-safe Telegram entity parsing for links

## 1.6.4 — 2026-07-17

### Fixed
- Instagram photos no longer flash caption/hashtags in status before send
- Photos sent once as a reply with no caption
- Inline link previews disabled for download results
- Photo `file_id` cache stores `is_image` correctly

## 1.6.3 — 2026-07-17

### Fixed
- Instagram videos no longer sent as poster photos with a play icon
- Reels / video posts go through yt-dlp again

### Added
- Admin **Clear media cache** for testing after updates

## 1.6.2 — 2026-07-17

### Changed
- Instagram images sent as normal Telegram photos again (not documents)

## 1.6.1 — 2026-07-17

### Changed
- Media sent without captions
- Prefer highest-resolution Instagram image CDN candidates

## 1.6.0 — 2026-07-17

### Added
- Instagram photo posts and carousels as Telegram albums
