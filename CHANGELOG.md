# Changelog

All notable changes to this bot are documented here.

## 1.8.11 — 2026-07-19

### Changed
- **Inline mode redesign** — works in chats the bot can't message:
  - First `@bot URL` prepares silently (spinner stays up to ~9s)
  - When ready, only **📤 Send** is shown (Telegram `file_id` cache)
  - Repeat of the same link is instant via cache (no re-download)
  - Same URL is never prepared twice while in-flight; carousels use the first item
  - No Preparing placeholder and no ready DMs
- Cached links pasted in chat skip Extracting/Uploading status — media is
  re-sent from the stored Telegram `file_id` immediately

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
