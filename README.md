# Clipper

Live stream clipper with automatic highlights. Point it at a live stream and it keeps a rolling
video buffer on disk. Clip the last N seconds with one click, or let it clip automatically when
chat goes off (Twitch, YouTube Live and Kick).

## How it works

```
streamlink ──► ffmpeg (stream copy) ──► 2s .ts segments (rolling buffer, default 180s)
                                               │
 Twitch / YouTube / Kick chat ──► hype detector ┤──► concat segments ──► clips/*.mp4 + thumbnail
                         ✂ button / API ───────┘
```

- **Recording**: `streamlink` pulls the stream and `ffmpeg` splits it into short segments without
  re-encoding, so the CPU cost stays low. Segments older than the buffer get deleted.
- **Clipping**: the segments covering `[t - pre, t + post]` are joined into an MP4. It waits for
  the post-roll before cutting.
- **Auto-highlights**: it reads the stream's live chat anonymously (no login needed) and compares the
  message rate over the last 10s with the 5-minute baseline. Messages containing hype keywords
  (`pog`, `kekw`, `clip`, `no way`, …) count double. When the recent rate passes
  `multiplier × baseline` (and at least `min_messages` arrive in the window), it saves a clip
  covering the 30s before and 10s after. A cooldown stops repeat clips of the same moment.
- Any site streamlink supports can be recorded and clipped manually. Auto-clipping needs a chat
  reader:

  | Platform | URL forms | How chat is read |
  |---|---|---|
  | Twitch | `twitch.tv/<channel>` | Anonymous IRC |
  | YouTube Live | `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/live/…`, `youtube.com/@name/live` | Polls the internal endpoint the chat popout uses (no API key or quota) |
  | Kick | `kick.com/<channel>` | Kick's public Pusher websocket |

  The YouTube and Kick readers use unofficial endpoints, so they can break if the sites change.
  Kick's channel lookup (`kick.com/api/v2/channels/<name>`) is sometimes blocked by Cloudflare. If
  that happens, set `CLIPPER_KICK_CHATROOM_<NAME>=<chatroom id>` to skip the lookup. If Kick changes
  its Pusher key, override it with `CLIPPER_KICK_PUSHER_KEY` / `CLIPPER_KICK_PUSHER_CLUSTER`.

## Run

### Docker

```bash
docker compose up -d --build
# open http://localhost:8000
```

Clips are written to `./data/clips`.

### Local

Requires Python 3.10+ and `ffmpeg` on `PATH` (streamlink is installed as a dependency).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
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
| `CLIPPER_MANUAL_PRE` / `_POST` | `30` / `5` | Manual clip window |
| `CLIPPER_AUTO_PRE` / `_POST` | `30` / `10` | Auto clip window |
| `CLIPPER_HYPE_WINDOW` | `10` | Seconds of "recent" chat |
| `CLIPPER_HYPE_BASELINE` | `300` | Seconds of baseline chat |
| `CLIPPER_HYPE_MULTIPLIER` | `3` | Spike threshold (lower = more clips) |
| `CLIPPER_HYPE_MIN_MESSAGES` | `15` | Minimum messages in window to trigger |
| `CLIPPER_HYPE_COOLDOWN` | `60` | Seconds between auto clips |
| `CLIPPER_HYPE_WARMUP` | `60` | Seconds of chat before auto clips start |
| `CLIPPER_HYPE_KEYWORDS` | `clip,pog,…` | Comma-separated words that count double |

## Tests

```bash
pytest
```

The integration test runs a synthetic ffmpeg stream, injects a chat burst, and checks that an auto
clip is cut.
