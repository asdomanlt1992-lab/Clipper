# Clipper

Live stream clipper with automatic highlights. Point it at a live stream and it keeps a rolling
video buffer on disk. Clip the last N seconds with one click, or let it clip automatically when
chat goes off (Twitch, YouTube Live and Kick).

## How it works

```
streamlink ──► ffmpeg (stream copy) ──► 2s .ts segments (rolling buffer, default 180s)
                                               │                 │
                                               │       loudness per segment (audio monitor)
                                               ▼                 ▼
 Twitch / YouTube / Kick chat ──► spike + mood ──► score 0-100 ──► gate ──► cut clip
                                                                            │
                     transcribe (faster-whisper) ──► Claude classifier ──► keep / reject
```

It runs on its own: moments are detected, cut, checked, and either kept or rejected without
anyone watching. You can still override any decision on the web page.

### 1. Moment detection

- **Chat spike**: it reads the stream's live chat anonymously (no login needed). When the
  message rate over the last 10s passes `multiplier × the 5-minute baseline`, that's a moment.
  Messages with hype words (`pog`, `kekw`, `no way`, …) count double.
- **Chat mood**: positive hype words are weighed against negative ones (`cringe`, `yikes`,
  `chill`, `calm down`, `mods`, …). A spike where chat is mostly negative is skipped. That catches
  a streamer yelling at chat, where chat reacts with "bro chill".
- **Audio**: the loudness of every segment is measured against the stream's normal level. On
  streams with chat, loudness only adds to the score and never triggers a clip by itself. On sites
  with no chat reader, a big audio spike can trigger a clip, but only when the Claude check is on to
  filter out bad ones (`CLIPPER_AUDIO_ONLY=auto`).

### 2. Score (0-100)

| Part | Points | Full marks when |
|---|---|---|
| Chat spike | up to 50 | chat hits 2× the trigger multiplier |
| Chat mood | up to 25 | all hype words (12.5 = neutral, 0 = all negative) |
| Audio | up to 25 | +12 dB over normal |

Moments below `MIN_SCORE` (30) aren't cut at all.

### 3. Analysis and verdict

After cutting, each clip is:

1. **Transcribed** locally with faster-whisper (if installed, which the Docker image does).
2. **Classified by Claude** (if `ANTHROPIC_API_KEY` is set). Claude gets 3 frames, the transcript
   and the chat from that moment, and returns a category (`highlight`, `skill`, `funny`, `fail`,
   `wholesome`, `rage`, `argument`, `technical`, `offtopic`, `sensitive`), keep yes/no, a title and
   a one-line summary.
3. **Given a verdict**:
   - with Claude: rejected if the category is in `REJECT_CATEGORIES` (default
     `rage,argument,technical,offtopic,sensitive`) or Claude says don't keep; kept otherwise.
     If Claude declines to review a clip, it's rejected.
   - without Claude, or if the call fails: kept if score ≥ `APPROVE_SCORE` (50), else rejected.
   - manual clips are always kept.

Rejected clips stay visible (with the reason) for `REJECTED_KEEP_HOURS` (24), then their video is
deleted. Set `CLIPPER_REVIEW_UNCERTAIN=true` to send low-confidence calls to a "Needs review" tab
instead of deciding.

**Cost**: one Claude Opus 5.5 call per auto clip at low effort (3 small images + transcript + chat),
roughly a cent each. Server-side refusal fallback is on, so a refused request is retried on another
model automatically.

### Minimum clip length

Every clip is at least `CLIPPER_MIN_CLIP_SECONDS` (31s) long:

- a request shorter than that gets more footage from before the moment
- if the stream started too recently to have that much footage, it keeps recording after the
  moment until the clip is long enough
- if the stream drops before then, the clip fails with a message instead of saving a short video

### Recording

- `streamlink` pulls the stream and `ffmpeg` splits it into short segments without re-encoding,
  so the CPU cost stays low. Clips are the segments covering `[t - pre, t + post]`, joined into an
  MP4.
- Any site streamlink supports can be recorded and clipped manually. Chat readers:

  | Platform | URL forms | How chat is read |
  |---|---|---|
  | Twitch | `twitch.tv/<channel>` | Anonymous IRC over WebSocket (port 443) |
  | YouTube Live | `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/live/…`, `youtube.com/@name/live` | Polls the internal endpoint the chat popout uses (no API key or quota) |
  | Kick | `kick.com/<channel>` | Kick's public Pusher websocket |

  The YouTube and Kick readers use unofficial endpoints, so they can break if the sites change.
  Kick's channel lookup is sometimes blocked by Cloudflare. If that happens, set
  `CLIPPER_KICK_CHATROOM_<NAME>=<chatroom id>`. If Kick changes its Pusher key, override it with
  `CLIPPER_KICK_PUSHER_KEY` / `CLIPPER_KICK_PUSHER_CLUSTER`.

## Signed-in video (cookies)

Some streams only play when you're signed in: YouTube sometimes asks to "confirm you're not a
bot", and sub-only or members-only streams need an account that can watch them. Chat never needs
this.

1. In a browser where you're signed in to YouTube and/or Twitch, export your cookies as
   `cookies.txt` (Netscape format), for example with the "Get cookies.txt LOCALLY" extension.
2. Upload it in the **Signed-in video** box on the page. It's saved to
   `data/secrets/cookies.txt` (owner-only permissions) and passed to streamlink. For Twitch, the
   `auth-token` cookie is also sent as the API authorization header.
3. Streams pick it up on their next reconnect (stop and re-add a stream to apply it right away).

Cookies are as sensitive as a password: anyone with the file can act as your account. Keep it on
your own machine and remove it when you're done.

`CLIPPER_STREAMLINK_ARGS` passes any extra streamlink options (e.g. `--hls-live-edge 2`).

## Posting to YouTube and TikTok

Every kept clip is turned into a **vertical 1080×1920 video** and uploaded to each connected
platform automatically.

- **Vertical version**: the whole frame centred over a blurred, zoomed copy of itself
  (`CLIPPER_VERTICAL_MODE=blur`), or a centre crop (`crop`). Word-by-word captions from the
  transcript are burned in (`CLIPPER_CAPTIONS=true`, needs transcription on).
- **Text**: Claude's title (plus `#Shorts` on YouTube), its one-line summary, "Clipped live from
  <channel>", and `CLIPPER_HASHTAGS`.
- **What gets posted**: only clips at least 31s long that Claude approved, that you clipped by
  hand, or that you clicked **Keep** / **Post** on. Clips kept by score alone (no Claude) wait for
  you (`CLIPPER_POST_REQUIRES_CLASSIFIER=true`).
- **Pacing**: at least `CLIPPER_POST_INTERVAL_MINUTES` (20) between posts per platform, and at most
  `CLIPPER_YOUTUBE_DAILY_LIMIT` (6) / `CLIPPER_TIKTOK_DAILY_LIMIT` (10) per 24h. Extra clips get
  scheduled for the next free slot. Failed uploads retry 3 times. If a platform reports its daily
  limit was hit, the upload retries 6 hours later.
- Each clip card shows per-platform status with a link to the post. **Post** / **Retry post**
  queues a clip by hand.

Only post streams you own or have permission to repost.

### YouTube setup (one time)

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Set up the OAuth consent screen (External; add yourself as a test user while the app is
   unverified).
3. Create an **OAuth client ID** of type *Web application*, with authorized redirect URI
   `<CLIPPER_PUBLIC_URL>/auth/youtube/callback` (e.g. `http://localhost:8000/auth/youtube/callback`).
4. Set `CLIPPER_YOUTUBE_CLIENT_ID` and `CLIPPER_YOUTUBE_CLIENT_SECRET`, restart, and click
   **Connect YouTube Shorts** on the page.

Notes: the default API quota is 10,000 units a day and an upload costs about 1,600, which is why the
daily limit defaults to 6. Request more quota from Google to raise it. Videos uploaded through an
unverified API project may be locked to private until Google audits the project.
`CLIPPER_YOUTUBE_PRIVACY` is `public`, `unlisted` or `private`.

### TikTok setup (one time)

1. At developers.tiktok.com, create an app and add **Login Kit** and **Content Posting API** with
   **Direct Post** enabled. Request the `user.info.basic` and `video.publish` scopes.
2. Register the redirect URI `<CLIPPER_PUBLIC_URL>/auth/tiktok/callback`. TikTok may require
   HTTPS and a real domain, so you may need to run Clipper behind a domain or an HTTPS tunnel, and
   set `CLIPPER_PUBLIC_URL` to it.
3. Set `CLIPPER_TIKTOK_CLIENT_KEY` and `CLIPPER_TIKTOK_CLIENT_SECRET`, restart, and click
   **Connect TikTok**.

Notes: until TikTok audits your app, posts can only be private (`SELF_ONLY`). Clipper detects this
and posts privately, and the clip card says so. After the audit, posts use `CLIPPER_TIKTOK_PRIVACY`
(default `PUBLIC_TO_EVERYONE`).

Sign-in tokens are stored in `data/secrets/` (owner-only permissions). **Disconnect** deletes them.

## Run

### Docker

Keys go in a `.env` file next to `docker-compose.yml` (it's git-ignored), one per line:

```
ANTHROPIC_API_KEY=sk-ant-...
CLIPPER_YOUTUBE_CLIENT_ID=...
CLIPPER_YOUTUBE_CLIENT_SECRET=...
```

```bash
docker compose up -d --build
# open http://localhost:8000
```

Clips are written to `./data/clips`. The Whisper model downloads on the first clip and is cached
in `./data/models`.

### Local

Requires Python 3.10+ and `ffmpeg` on `PATH` (streamlink is installed as a dependency).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[ai,dev]'   # drop "ai" to skip Claude + transcription
export ANTHROPIC_API_KEY=sk-ant-...   # optional
clipper            # or: python -m clipper
```

Paste a URL such as `https://twitch.tv/somechannel`, then click **Watch**.

To try it without a real stream, use a synthetic source: `lavfi:testsrc=size=640x360:rate=30`.
`direct:<url-or-file>` sends a URL or file straight to ffmpeg.

## API

| Method | Path | Body |
|---|---|---|
| GET | `/api/streams` | – |
| POST | `/api/streams` | `{"url": "...", "quality": "best", "auto": true, "multiplier": 3}` |
| PATCH | `/api/streams/{id}` | `{"auto": false, "multiplier": 4}` |
| DELETE | `/api/streams/{id}` | – |
| POST | `/api/streams/{id}/clip` | `{"pre": 30, "post": 5, "title": "..."}` |
| GET | `/api/clips` | – |
| POST | `/api/clips/{id}/post` | – (keep + queue for posting) |
| GET | `/api/platforms` | – |
| DELETE | `/api/platforms/{youtube\|tiktok}` | – (disconnect) |
| POST | `/api/clips/{id}/verdict` | `{"verdict": "approved" \| "rejected"}` |
| DELETE | `/api/clips/{id}` | – |

Clip files are served from `/media/<file>`. Watched streams are saved in `data/streams.json` and
resume after a restart.

Example, a hotkey or Stream Deck button:

```bash
curl -XPOST localhost:8000/api/streams/<id>/clip -H 'content-type: application/json' -d '{}'
```

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `CLIPPER_DATA_DIR` | `data` | Buffer and clip storage |
| `CLIPPER_PORT` / `CLIPPER_HOST` | `8000` / `0.0.0.0` | Web server |
| `CLIPPER_BUFFER_SECONDS` | `180` | Rolling buffer length |
| `CLIPPER_SEGMENT_SECONDS` | `2` | Segment size (clip boundary granularity) |
| `CLIPPER_QUALITY` | `best` | Default streamlink quality |
| `CLIPPER_RECONNECT_SECONDS` | `30` | Retry interval when a stream is offline |
| `CLIPPER_MIN_CLIP_SECONDS` | `31` | Every clip is at least this long (see below) |
| `CLIPPER_MANUAL_PRE` / `_POST` | `30` / `5` | Manual clip window |
| `CLIPPER_AUTO_PRE` / `_POST` | `30` / `10` | Auto clip window |
| `CLIPPER_HYPE_WINDOW` | `10` | Seconds of "recent" chat |
| `CLIPPER_HYPE_BASELINE` | `300` | Seconds of baseline chat |
| `CLIPPER_HYPE_MULTIPLIER` | `3` | Spike threshold (lower = more clips) |
| `CLIPPER_HYPE_MIN_MESSAGES` | `15` | Minimum messages in window to trigger |
| `CLIPPER_HYPE_COOLDOWN` | `60` | Seconds between auto clips |
| `CLIPPER_HYPE_WARMUP` | `60` | Seconds of chat before auto clips start |
| `CLIPPER_HYPE_KEYWORDS` | `clip,pog,…` | Positive words (count double, raise mood) |
| `CLIPPER_HYPE_NEGATIVE_KEYWORDS` | `yikes,cringe,…` | Negative words (lower mood) |
| `CLIPPER_MIN_SCORE` | `30` | Moments below this aren't cut |
| `CLIPPER_MOOD_SKIP` | `-0.5` | Skip chat spikes with mood at or below this (-1..1) |
| `CLIPPER_APPROVE_SCORE` | `50` | Keep threshold when Claude isn't available |
| `CLIPPER_REVIEW_UNCERTAIN` | `false` | Send borderline calls to "Needs review" |
| `CLIPPER_REJECTED_KEEP_HOURS` | `24` | Delete rejected videos after this (0 = right away) |
| `CLIPPER_AUDIO` | `true` | Track loudness |
| `CLIPPER_AUDIO_TRIGGER_DB` | `10` | dB above normal for an audio-only trigger |
| `CLIPPER_AUDIO_ONLY` | `auto` | Audio-only triggers on chatless sites: `auto` (only with Claude), `on`, `off` |
| `CLIPPER_TRANSCRIBE` | `auto` | `auto` (if faster-whisper is installed), `on`, `off` |
| `CLIPPER_WHISPER_MODEL` | `base` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `CLIPPER_WHISPER_DEVICE` | `auto` | `cpu`, `cuda`, `auto` |
| `CLIPPER_CLASSIFIER` | `auto` | `auto` (if `ANTHROPIC_API_KEY` is set), `on`, `off` |
| `CLIPPER_CLAUDE_MODEL` | `claude-opus-5-5` | Model for the clip check |
| `CLIPPER_CLAUDE_EFFORT` | `low` | `low`, `medium`, `high` |
| `CLIPPER_PUBLIC_URL` | `http://localhost:8000` | Base URL for sign-in redirects |
| `CLIPPER_VERTICAL` / `_MODE` | `true` / `blur` | Make 9:16 versions (`blur` or `crop`) |
| `CLIPPER_CAPTIONS` | `true` | Burn in captions |
| `CLIPPER_HASHTAGS` | `shorts,gaming,clips,livestream` | Hashtags for posts |
| `CLIPPER_POST_REQUIRES_CLASSIFIER` | `true` | Only auto-post clips Claude approved |
| `CLIPPER_POST_INTERVAL_MINUTES` | `20` | Minimum gap between posts per platform |
| `CLIPPER_POST_YOUTUBE` / `_TIKTOK` | `true` | Turn posting to a platform on/off |
| `CLIPPER_YOUTUBE_CLIENT_ID` / `_SECRET` | – | Google OAuth client |
| `CLIPPER_YOUTUBE_PRIVACY` | `public` | `public`, `unlisted`, `private` |
| `CLIPPER_YOUTUBE_DAILY_LIMIT` | `6` | Max uploads per 24h |
| `CLIPPER_YOUTUBE_CATEGORY` | `20` | Category id (20 = Gaming) |
| `CLIPPER_TIKTOK_CLIENT_KEY` / `_SECRET` | – | TikTok app credentials |
| `CLIPPER_TIKTOK_PRIVACY` | `PUBLIC_TO_EVERYONE` | Used once your app is audited |
| `CLIPPER_TIKTOK_DAILY_LIMIT` | `10` | Max posts per 24h |
| `CLIPPER_REJECT_CATEGORIES` | `rage,argument,technical,offtopic,sensitive` | Categories that are always rejected |

## Tests

```bash
pytest
```

The integration tests run a synthetic ffmpeg stream with fake chat and check three things: a hype
burst gets kept, a negative burst gets skipped, and a rage clip gets rejected by a mocked Claude
check.
