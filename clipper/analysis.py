"""After a clip is cut: transcribe it, ask Claude what kind of moment it is, decide keep/reject.

Every step is optional and degrades gracefully:
  - no faster-whisper installed  -> no transcript
  - no Anthropic credentials     -> verdict from the 0-100 score alone
  - classifier error or refusal  -> verdict from the score (refusals are rejected)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

CATEGORIES = ["highlight", "skill", "funny", "fail", "wholesome",
              "rage", "argument", "technical", "offtopic", "sensitive"]

SYSTEM_PROMPT = """You review short clips captured automatically from live streams. A clip was \
saved because chat activity or the streamer's volume spiked. Your job is to say what kind of \
moment it is and whether it is worth keeping as a highlight that could be shared publicly.

Categories:
- highlight: an exciting moment (big play, win, reveal, surprise)
- skill: impressive gameplay or talent
- funny: a joke, bit, or comedic accident
- fail: a funny or dramatic loss, mistake, or death (usually good content)
- wholesome: a heartwarming or celebratory moment (milestone, thank-you, raid welcome)
- rage: the streamer yelling in anger at chat, mods, other people, or themselves
- argument: a real dispute or heated exchange with someone
- technical: stream problems (frozen video, black screen, broken audio, crash, setup trouble)
- offtopic: nothing happening (AFK, break screen, waiting, reading chat quietly)
- sensitive: anything that could embarrass or harm the streamer or others if shared: personal \
or private information, harassment, slurs, health or family emergencies, someone visibly upset

Loud does not mean good. Chat spamming laughter or hype words suggests a good moment; chat \
telling the streamer to calm down, or spamming concern, suggests a bad one. Weigh the \
transcript, the frames and the chat together. If unsure whether a moment would embarrass the \
streamer, choose sensitive and keep=false.

Set keep=true only if a viewer who missed the stream would enjoy this clip and the streamer \
would be comfortable seeing it shared. The title should be a short, catchy clip title (under \
60 characters, no hashtags) describing what happens."""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "keep": {"type": "boolean"},
        "confidence": {"type": "number", "description": "0 to 1"},
        "title": {"type": "string"},
        "summary": {"type": "string", "description": "One sentence describing what happens."},
        "reason": {"type": "string", "description": "One sentence on why keep or not."},
    },
    "required": ["category", "keep", "confidence", "title", "summary", "reason"],
    "additionalProperties": False,
}


@dataclass
class Analysis:
    transcript: str = ""
    words: list[list] = field(default_factory=list)  # [start, end, word] for captions
    classification: dict | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- transcription
_whisper_model = None
_whisper_lock = asyncio.Lock()


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _transcribe_sync(path: str, settings: Settings) -> tuple[str, list[list]]:
    global _whisper_model
    from faster_whisper import WhisperModel
    if _whisper_model is None:
        log.info("loading whisper model %s (first run downloads it)", settings.whisper_model)
        _whisper_model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                                      compute_type="int8")
    segments, _ = _whisper_model.transcribe(path, vad_filter=True, word_timestamps=True)
    lines, words = [], []
    for seg in segments:
        m, s = divmod(int(seg.start), 60)
        text = seg.text.strip()
        if text:
            lines.append(f"[{m:02d}:{s:02d}] {text}")
        for w in seg.words or []:
            words.append([round(w.start, 2), round(w.end, 2), w.word.strip()])
    return "\n".join(lines), words


async def transcribe(path: Path, settings: Settings) -> tuple[str, list[list]]:
    async with _whisper_lock:  # one at a time; it's CPU heavy
        return await asyncio.to_thread(_transcribe_sync, str(path), settings)


# ---------------------------------------------------------------- frames
async def extract_frames(video: Path, duration: float, count: int = 3) -> list[bytes]:
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            t = max(0.0, duration * (i + 1) / (count + 1))
            out = Path(tmp) / f"{i}.jpg"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            if out.exists():
                frames.append(out.read_bytes())
    return frames


# ---------------------------------------------------------------- classifier
_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.AsyncAnthropic()
    return _client


def build_user_content(context: dict, transcript: str, frames: list[bytes]) -> list[dict]:
    content: list[dict] = []
    for i, jpg in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i + 1} of {len(frames)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpg).decode()}})
    chat = "\n".join(context.get("chat", [])[-150:]) or "(no chat captured)"
    content.append({"type": "text", "text": (
        f"Platform: {context.get('platform') or 'unknown'}\n"
        f"Channel: {context.get('channel')}\n"
        f"Why it was captured: {context.get('trigger')}\n"
        f"Clip length: {context.get('duration', 0):.0f}s\n"
        f"Signal scores: {json.dumps(context.get('score', {}))}\n\n"
        f"<transcript>\n{transcript or '(no speech detected or transcription unavailable)'}\n</transcript>\n\n"
        f"<chat>\n{chat}\n</chat>\n\n"
        "Classify this clip.")})
    return content


async def classify(context: dict, transcript: str, frames: list[bytes], settings: Settings) -> dict:
    import anthropic
    client = _get_client()
    try:
        response = await client.beta.messages.create(
            model=settings.claude_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_content(context, transcript, frames)}],
            output_config={"effort": settings.claude_effort,
                           "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as exc:
        raise RuntimeError(f"Claude rate limited: {exc.message}") from exc
    except anthropic.AuthenticationError as exc:
        raise RuntimeError("Claude credentials rejected (check ANTHROPIC_API_KEY)") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"cannot reach Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        return {"category": "sensitive", "keep": False, "confidence": 1.0, "title": "",
                "summary": "", "reason": "The classifier declined to review this clip.",
                "refused": True}
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0))))
    return data


# ---------------------------------------------------------------- pipeline
async def analyze(video: Path, duration: float, context: dict, settings: Settings) -> Analysis:
    result = Analysis()
    want_transcript = settings.transcribe == "on" or (
        settings.transcribe == "auto" and whisper_available())
    if want_transcript:
        try:
            result.transcript, result.words = await transcribe(video, settings)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"transcription failed: {exc}")
            log.warning("transcription failed: %s", exc)
    if settings.classifier_enabled:
        try:
            frames = await extract_frames(video, duration)
            result.classification = await classify(context, result.transcript, frames, settings)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"classifier failed: {exc}")
            log.warning("classifier failed: %s", exc)
    return result


def decide(reason: str, score: float, analysis: Analysis, settings: Settings) -> tuple[str, str]:
    """Return (verdict, explanation). verdict: approved | rejected | review."""
    if reason == "manual":
        return "approved", "manual clip"
    cls = analysis.classification
    if cls:
        category = cls.get("category", "")
        if cls.get("refused"):
            return "rejected", "classifier declined to review it"
        if category in settings.reject_categories:
            return "rejected", f"classified as {category}: {cls.get('reason', '')}"
        if not cls.get("keep"):
            return "rejected", cls.get("reason", "classifier said not worth keeping")
        if settings.review_uncertain and cls.get("confidence", 1) < 0.6:
            return "review", f"low confidence {category}: {cls.get('reason', '')}"
        return "approved", f"{category}: {cls.get('reason', '')}"
    if settings.review_uncertain and settings.approve_score - 15 <= score < settings.approve_score:
        return "review", f"score {score:.0f} is borderline (no classifier)"
    if score >= settings.approve_score:
        return "approved", f"score {score:.0f} >= {settings.approve_score:.0f}"
    return "rejected", f"score {score:.0f} < {settings.approve_score:.0f}"
