"""9:16 vertical version of a clip for YouTube Shorts / TikTok, with burned-in captions."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

W, H = 1080, 1920


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_chunks(words: list[list], max_words: int = 3, max_gap: float = 0.6) -> list[tuple[float, float, str]]:
    """Group [start, end, word] into short on-screen chunks (TikTok-style)."""
    chunks: list[tuple[float, float, str]] = []
    cur: list[list] = []
    for w in words:
        if cur and (len(cur) >= max_words or w[0] - cur[-1][1] > max_gap):
            chunks.append((cur[0][0], cur[-1][1], " ".join(x[2].strip() for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        chunks.append((cur[0][0], cur[-1][1], " ".join(x[2].strip() for x in cur)))
    return [(s, e, t) for s, e, t in chunks if t]


def build_ass(chunks: list[tuple[float, float, str]], margin_v: int) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{_ass_time(s)},{_ass_time(max(e, s + 0.4))},Cap,,0,0,0,,{_ass_escape(t.upper())}"
             for s, e, t in chunks]
    return head + "\n".join(lines) + "\n"


def filter_graph(mode: str, subs: Path | None) -> tuple[str, int]:
    if mode == "crop":
        g = f"[0:v]scale=-2:{H},crop={W}:{H}[v0]"
        margin = 360
    else:  # blur: whole frame, centered, over a blurred zoomed copy
        g = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H},boxblur=20:2[bg];[b]scale={W}:-2[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
        margin = 520  # just under the centered video
    if subs:
        g += f";[v0]subtitles=filename={subs}[v]"
    else:
        g += ";[v0]null[v]"
    return g, margin


async def render_vertical(src: Path, out: Path, words: list[list] | None,
                          mode: str = "blur", captions: bool = True) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subs = None
        chunks = caption_chunks(words or []) if captions else []
        _, margin = filter_graph(mode, None)
        if chunks:
            subs = Path(tmp) / "captions.ass"
            subs.write_text(build_ass(chunks, margin), encoding="utf-8")
        graph, _ = filter_graph(mode, subs)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"vertical render failed: {msg[-1] if msg else proc.returncode}")
