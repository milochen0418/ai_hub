"""The talking AI's voice, on the hub's backend: it hears with Whisper and speaks with Kokoro.

    GET  /api/talk/status                    how ready the voice is (the models download on first use)
    POST /api/talk/tts   {"text", "lang"}    the words as a WAV (24 kHz, mono)
    WS   /api/talk/stt?lang=zh-TW            the microphone in (16 kHz 16-bit PCM), words out:
                                             {"type": "subtitle", "seg", "text", "final"}
                                             {"type": "status", "phase", "progress", "message"}

Speaking: Kokoro-82M v1.1-zh (ONNX, ``kokoro-onnx``), which has English and Chinese voices.
Chinese goes through misaki's Chinese G2P (English words in it through espeak), English through
espeak as ``kokoro-onnx`` does; a sentence with any CJK character is read in Chinese. The models
(about 380 MB) download from the kokoro-onnx releases into ``<AI_HUB_DATA>/voice/`` the first time
the voice is wanted; the vocabulary config ships with the hub (``data/``, Apache-2.0).

Hearing: faster-whisper (``AI_HUB_WHISPER_MODEL``, default ``small``, about 480 MB, downloaded
once into ``<AI_HUB_DATA>/voice/whisper/``), as reflex_ddns_livekit_english_chat does it: the
browser streams its microphone, a pause ends a segment (by loudness), a growing segment is
transcribed again about once a second (partial), and once more carefully at the pause (final).
Traditional Chinese (``lang=zh-TW``) comes out in Traditional characters (OpenCC).

Both load lazily in threads (``start``) and say how far they are (``status``); the browser uses
its own speech while they aren't ready, or on a Python without their wheels (3.15 as of now).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import json
import logging
import os
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ai_hub import config

logger = logging.getLogger(__name__)

STATUS_PATH = "/api/talk/status"
TTS_PATH = "/api/talk/tts"
STT_PATH = "/api/talk/stt"

KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
KOKORO_MODEL = "kokoro-v1.1-zh.onnx"
KOKORO_VOICES = "voices-v1.1-zh.bin"
KOKORO_VOCAB = Path(__file__).parent / "data" / "kokoro-v1.1-zh.config.json"
# The voices of kokoro-v1.1-zh: women and men, Chinese and English.
VOICES = {
    "zh": {"female": "zf_001", "male": "zm_010"},
    "en": {"female": "af_maple", "male": "bf_vale"},
}
DEFAULT_VOICE = {"zh": os.environ.get("AI_HUB_VOICE_ZH") or VOICES["zh"]["female"], "en": os.environ.get("AI_HUB_VOICE_EN") or VOICES["en"]["female"]}
SAMPLE_RATE = 24000
MAX_TTS_CHARS = 600

WHISPER_MODEL = os.environ.get("AI_HUB_WHISPER_MODEL", "").strip() or "small"
WHISPER_THREADS = int(os.environ.get("AI_HUB_WHISPER_THREADS", "") or min(8, os.cpu_count() or 4))
WHISPER_WORKERS = 2
_WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

RATE = 16000
FRAME = RATE * 30 // 1000  # 30 ms: the unit of the pause detector
PREROLL_FRAMES = 10  # 300 ms kept from before the speech starts, so the first word is whole
END_SILENCE_S = 0.8  # a pause this long ends a segment
MAX_SEGMENT_S = 20.0  # longer speech is cut into segments anyway
MIN_SPEECH_S = 0.3  # shorter sounds (a cough, a click) are dropped
PARTIAL_EVERY_S = 1.0  # how often a growing segment is transcribed again
IDLE_END_S = 1.0  # no audio for this long (muted, tab asleep) ends a segment

MOODS = ("neutral", "happy", "laughing", "surprised", "thinking", "sad", "angry", "wink")
# What a model may write instead of the English tag.
_MOOD_WORDS = {
    "開心": "happy", "高興": "happy", "快樂": "happy", "微笑": "happy", "happy": "happy", "glad": "happy", "smile": "happy",
    "大笑": "laughing", "笑": "laughing", "laughing": "laughing", "laugh": "laughing", "lol": "laughing",
    "驚訝": "surprised", "驚喜": "surprised", "surprised": "surprised", "surprise": "surprised", "wow": "surprised",
    "思考": "thinking", "想想": "thinking", "thinking": "thinking", "hmm": "thinking", "curious": "thinking",
    "難過": "sad", "傷心": "sad", "sad": "sad", "sorry": "sad",
    "生氣": "angry", "angry": "angry", "annoyed": "angry",
    "眨眼": "wink", "wink": "wink",
    "平靜": "neutral", "中性": "neutral", "neutral": "neutral", "calm": "neutral",
}
_TAG = re.compile(r"^\s*[\[【（(]\s*([^\]】）)\n]{1,16}?)\s*[\]】）)]\s*[:：,，]?\s*")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def voice_dir() -> Path:
    path = config.data_dir() / "voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------ words


def mood_of(reply: str) -> tuple[str, str]:
    """The mood tag a reply starts with (``[happy] ...``), and the words without it."""
    m = _TAG.match(reply or "")
    if m:
        word = m.group(1).strip().lower()
        mood = _MOOD_WORDS.get(word) or (word if word in MOODS else "")
        if mood:
            return mood, reply[m.end():].strip()
    return "neutral", (reply or "").strip()


def spoken(text: str) -> str:
    """Words as a voice should read them: no markdown, no code, no addresses."""
    s = re.sub(r"```.*?```", " (code) ", text or "", flags=re.S)
    s = re.sub(r"`([^`\n]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)  # pictures
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # links: their words
    s = re.sub(r"https?://\S+", lambda m: "連結" if _CJK.search(s) else "a link", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)  # headings
    s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", s, flags=re.M)  # bullets
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s, flags=re.S)
    s = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", s)
    s = s.replace("|", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def sentences(text: str, limit: int = 220) -> list[str]:
    """The words cut into sentences for the voice, each at most ``limit`` characters, so the
    first one plays while the next are made."""
    out: list[str] = []
    for line in spoken(text).split("\n"):
        pieces = [p for p in re.split(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9\"“(])", line.strip()) if p and p.strip()]
        for piece in pieces:
            piece = piece.strip()
            while len(piece) > limit:
                cut = max(piece.rfind(c, 0, limit) for c in "，,、：:")
                if cut < limit // 3:
                    cut = limit
                out.append(piece[:cut + 1].strip() if cut < limit else piece[:limit].strip())
                piece = piece[cut + 1:].strip() if cut < limit else piece[limit:].strip()
            if piece:
                # A fragment of a few characters (no sentence of its own) joins the one before it.
                if out and len(piece) < 6 and not re.search(r"[.!?。！？]$", piece) and not _CJK.search(piece) and len(out[-1]) + len(piece) <= limit:
                    out[-1] = f"{out[-1]} {piece}"
                else:
                    out.append(piece)
    return out


def language_of(text: str, hint: str = "") -> str:
    """``zh`` for words with any CJK character, else ``en``."""
    if _CJK.search(text or ""):
        return "zh"
    return "en"


def context_text(raw: Any, limit: int = 1500) -> str:
    """What a page says it shows (JSON from the dialog's form, or already a value) as words
    for the model: its ``text`` when it has one, else the JSON itself."""
    data = raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
    if data is None or data == "":
        return ""
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        text = data["text"]
    elif isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit]


_s2tw = None


def traditional(text: str, lang: str) -> str:
    """Chinese in Traditional characters for a page in 繁體中文 (a model may answer in Simplified)."""
    global _s2tw
    if not text or lang.lower() not in ("zh-tw", "zh-hant", "zh-hk") or not _CJK.search(text):
        return text
    if _s2tw is None:
        try:
            import opencc

            _s2tw = opencc.OpenCC("s2twp")
        except ImportError:
            return text
    return _s2tw.convert(text)


def wav_bytes(samples: Any, rate: int = SAMPLE_RATE) -> bytes:
    """Float samples (-1..1) as a 16-bit mono WAV."""
    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def _speed_for_zh(phonemes: int) -> float:
    # As in Kokoro's own Chinese sample: long inputs rush, so slow down a little as they grow.
    if phonemes <= 83:
        return 1.0
    if phonemes < 183:
        return 1.0 - (phonemes - 83) / 500
    return 0.8


# ------------------------------------------------------------ downloads


def _download(url: str, path: Path, progress: Callable[[int, int], None]) -> None:
    """Fetches ``url`` into ``path`` (through a ``.part`` file), reporting bytes done and total."""
    import httpx

    part = path.with_suffix(path.suffix + ".part")
    with httpx.Client(follow_redirects=True, timeout=60.0) as client, client.stream("GET", url) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    os.replace(part, path)


class _Engine:
    """A model that loads in its own thread, once, and says how it goes."""

    name = "model"

    def __init__(self) -> None:
        self.phase = "off"  # off | downloading | loading | ready | error | unavailable
        self.message = ""
        self.done = 0
        self.total = 0
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name=f"{self.name}-load", daemon=True).start()

    def _run(self) -> None:
        try:
            self._load()
            self.phase = "ready"
            self.message = ""
            logger.info("%s ready", self.name)
        except ImportError as e:
            self.phase, self.message = "unavailable", f"{self.name}: its packages aren't installed ({e})"
            logger.warning("%s", self.message)
        except Exception as e:  # noqa: BLE001 — shown in the dialog
            self.phase, self.message = "error", f"{self.name}: {e}"
            logger.exception("%s unavailable", self.name)

    def _load(self) -> None:
        raise NotImplementedError

    def _progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total

    def progress(self) -> str:
        if self.phase != "downloading" or not self.total:
            return ""
        return f"{self.done / 1e6:.0f} / {self.total / 1e6:.0f} MB"

    def status(self) -> dict[str, Any]:
        return {"phase": self.phase, "progress": self.progress(), "message": self.message}


# ------------------------------------------------------------ speaking


class Voice(_Engine):
    name = "Kokoro (the voice)"

    def __init__(self) -> None:
        super().__init__()
        self.kokoro = None
        self.g2p = None
        self.t2s = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kokoro")

    def _fetch(self) -> tuple[Path, Path]:
        model, voices = voice_dir() / KOKORO_MODEL, voice_dir() / KOKORO_VOICES
        for path in (model, voices):
            if path.exists():
                continue
            self.phase = "downloading"
            logger.info("Downloading %s into %s", path.name, path.parent)
            _download(KOKORO_URL + path.name, path, self._progress)
        return model, voices

    def _load(self) -> None:
        from kokoro_onnx import Kokoro
        import opencc

        model, voices = self._fetch()
        self.phase = "loading"
        kokoro = Kokoro(str(model), str(voices), vocab_config=str(KOKORO_VOCAB))
        try:
            from misaki import zh
        except ImportError:  # misaki stops at Python 3.12: Chinese is then the browser's to read
            self.g2p = None
        else:
            self.t2s = opencc.OpenCC("t2s")
            # English words inside Chinese: espeak's phonemes, as for English sentences.
            self.g2p = zh.ZHG2P(version="1.1", en_callable=lambda text: kokoro.tokenizer.phonemize(text, "en-us"))
        self.kokoro = kokoro

    def _say(self, text: str, lang: str, voice: str, speed: float):
        if lang == "zh":
            if self.g2p is None:
                raise RuntimeError("Chinese needs misaki, which this Python has no wheel for")
            phonemes, _ = self.g2p(self.t2s.convert(text))
            phonemes = phonemes.replace("❓", "").strip()
            if not phonemes:
                raise ValueError("nothing to say")
            return self.kokoro.create(phonemes, voice=voice, speed=speed * _speed_for_zh(len(phonemes)), is_phonemes=True)
        return self.kokoro.create(text, voice=voice, speed=speed, lang="en-us")

    async def say(self, text: str, lang: str = "", voice: str = "", speed: float = 1.0) -> bytes:
        """The words as a WAV. ``lang`` is a hint; a sentence with CJK characters is Chinese."""
        if self.phase != "ready" or self.kokoro is None:
            raise RuntimeError(self.message or "the voice isn't ready")
        lang = language_of(text)
        voice = voice if voice in (self.kokoro.voices or {}) else DEFAULT_VOICE[lang]
        speed = min(1.6, max(0.6, float(speed or 1.0)))
        loop = asyncio.get_running_loop()
        samples, rate = await loop.run_in_executor(self._pool, self._say, text[:MAX_TTS_CHARS], lang, voice, speed)
        return wav_bytes(samples, rate)

    def status(self) -> dict[str, Any]:
        return {**super().status(), "voices": VOICES, "default": DEFAULT_VOICE, "zh": self.phase != "ready" or self.g2p is not None}


# ------------------------------------------------------------ hearing


class Ears(_Engine):
    name = "Whisper (the ears)"

    def __init__(self) -> None:
        super().__init__()
        self.model = None
        self.s2tw = None
        self._pool = ThreadPoolExecutor(max_workers=WHISPER_WORKERS, thread_name_prefix="whisper")

    def _fetch(self) -> str:
        import huggingface_hub
        from faster_whisper.utils import _MODELS

        repo = WHISPER_MODEL if "/" in WHISPER_MODEL else _MODELS.get(WHISPER_MODEL)
        if not repo:
            raise ValueError(f"unknown Whisper model {WHISPER_MODEL!r} (one of {', '.join(_MODELS)}, or a Hugging Face repo)")
        files = {"repo_id": repo, "cache_dir": str(voice_dir() / "whisper"), "allow_patterns": _WHISPER_FILES}
        try:
            return huggingface_hub.snapshot_download(**files, local_files_only=True)
        except Exception:  # noqa: BLE001 — not downloaded yet
            pass
        self.phase = "downloading"
        logger.info("Downloading the speech model %s", repo)
        from tqdm import tqdm

        engine = self

        class Bar(tqdm):
            """The bytes so far of the biggest file (model.bin is nearly all of the model)."""

            def __init__(self, *args, **kwargs) -> None:
                kwargs["file"] = io.StringIO()
                super().__init__(*args, **kwargs)

            def update(self, n: int = 1) -> None:
                super().update(n)
                if self.unit == "B" and self.total and self.total >= engine.total:
                    engine._progress(self.n, self.total)

            def update_transfer(self, n: int = 1) -> None:  # Xet downloads report here
                self.update(n)

            def set_transfer_postfix_str(self, *args, **kwargs) -> None:
                pass

        return huggingface_hub.snapshot_download(**files, tqdm_class=Bar)

    def _load(self) -> None:
        from faster_whisper import WhisperModel
        import opencc

        path = WHISPER_MODEL if os.path.isdir(WHISPER_MODEL) else self._fetch()
        self.phase = "loading"
        self.s2tw = opencc.OpenCC("s2twp")
        self.model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS, num_workers=WHISPER_WORKERS)

    def _run_model(self, audio: Any, prompt: str, final: bool, language: str | None) -> str:
        segments, _info = self.model.transcribe(
            audio,
            language=language,
            beam_size=3 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0,
            without_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, audio: Any, prompt: str, *, final: bool, lang: str = "") -> str:
        language = "zh" if lang.lower().startswith("zh") else "en" if lang.lower().startswith("en") else None
        if language == "zh" and not prompt and lang.lower() in ("zh-tw", "zh-hant", "zh-hk"):
            prompt = "以下是繁體中文的對話。"
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(self._pool, self._run_model, audio, prompt, final, language)
        if text and lang.lower() in ("zh-tw", "zh-hant", "zh-hk") and self.s2tw is not None:
            text = self.s2tw.convert(text)
        return text


voice = Voice()
ears = Ears()


def start() -> None:
    """Loads both (once), from the first dialog that talks."""
    voice.start()
    ears.start()


def status() -> dict[str, Any]:
    return {"tts": voice.status(), "stt": {**ears.status(), "model": WHISPER_MODEL}}


# ------------------------------------------------------------ the microphone


class _Speaker:
    """Cuts one microphone into segments at its pauses, by loudness."""

    def __init__(self, on_start: Callable[[], None], on_end: Callable[[Any], None]) -> None:
        import numpy as np

        self._np = np
        self._on_start = on_start
        self._on_end = on_end
        self._noise = 0.003  # loudness of the background, followed while nobody speaks
        self._rest = np.zeros(0, np.float32)  # samples short of a frame
        self._preroll: collections.deque = collections.deque(maxlen=PREROLL_FRAMES)
        self._frames: list = []
        self._voiced = 0  # frames with speech in the segment
        self._quiet = 0  # frames of silence since the last speech
        self.speaking = False

    @property
    def speech_s(self) -> float:
        return self._voiced * FRAME / RATE

    def audio(self):
        np = self._np
        return np.concatenate(self._frames) if self._frames else np.zeros(0, np.float32)

    def feed(self, samples) -> None:
        np = self._np
        data = np.concatenate([self._rest, samples]) if self._rest.size else samples
        whole = len(data) // FRAME * FRAME
        self._rest = data[whole:]
        for frame in data[:whole].reshape(-1, FRAME):
            level = float(np.sqrt(np.mean(frame * frame)))
            voiced = level > max(0.008, self._noise * 3.5)
            if not voiced:
                self._noise += (min(level, 0.05) - self._noise) * 0.05
            if not self.speaking:
                self._preroll.append(frame)
                if voiced:
                    self.speaking = True
                    self._frames = list(self._preroll)
                    self._preroll.clear()
                    self._voiced, self._quiet = 1, 0
                    self._on_start()
                continue
            self._frames.append(frame)
            if voiced:
                self._voiced += 1
                self._quiet = 0
            else:
                self._quiet += 1
            if self._quiet * FRAME >= END_SILENCE_S * RATE or len(self._frames) * FRAME >= MAX_SEGMENT_S * RATE:
                self.finish()

    def finish(self) -> None:
        """End the segment being spoken (its audio, or None for a mere blip)."""
        if not self.speaking:
            return
        keep = len(self._frames) - max(0, self._quiet - 10)  # 300 ms of the pause that ended it
        audio = self._np.concatenate(self._frames[:keep]) if keep > 0 else None
        enough = self.speech_s >= MIN_SPEECH_S
        self.speaking = False
        self._frames = []
        self._voiced = self._quiet = 0
        self._on_end(audio if enough else None)


class _Session:
    """One browser's microphone: audio in, words out."""

    def __init__(self, ws: WebSocket, lang: str) -> None:
        self.ws = ws
        self.lang = lang
        self.speaker = _Speaker(self._started, self._ended)
        self.seg = 0
        self.finals: collections.deque = collections.deque()
        self.partial = None
        self.partial_at = 0.0
        self.shown: set[int] = set()
        self.context = ""
        self.last_audio = time.monotonic()
        self.sent = ""
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()

    def _started(self) -> None:
        self.partial_at = time.monotonic()

    def _ended(self, audio) -> None:
        seg = self.seg
        self.seg += 1
        if audio is not None or seg in self.shown:
            self.finals.append((seg, audio))
            self.wake.set()

    async def _send(self, data: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    async def _send_status(self) -> None:
        now = {"type": "status", **ears.status()}
        key = json.dumps(now)
        if key == self.sent:
            return
        self.sent = key
        await self._send(now)

    async def _receive(self) -> None:
        import numpy as np

        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if not data:
                continue
            self.last_audio = time.monotonic()
            if ears.phase == "ready":
                usable = len(data) // 2 * 2
                self.speaker.feed(np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            await self._send_status()
            if not self.speaker.speaking:
                continue
            now = time.monotonic()
            if now - self.last_audio > IDLE_END_S:
                self.speaker.finish()
            elif now - self.partial_at >= PARTIAL_EVERY_S and self.speaker.speech_s >= 2 * MIN_SPEECH_S:
                self.partial = (self.seg, self.speaker.audio())
                self.partial_at = now
                self.wake.set()

    async def _work(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.finals or self.partial:
                if self.finals:
                    seg, audio = self.finals.popleft()
                    text = await ears.transcribe(audio, self.context, final=True, lang=self.lang) if audio is not None else ""
                    if text:
                        self.context = text[-200:]
                    if text or seg in self.shown:
                        await self._send({"type": "subtitle", "seg": seg, "text": text, "final": True})
                    self.shown.discard(seg)
                    continue
                seg, audio = self.partial
                self.partial = None
                if seg != self.seg or not self.speaker.speaking:
                    continue
                text = await ears.transcribe(audio, self.context, final=False, lang=self.lang)
                if text and seg == self.seg and self.speaker.speaking:
                    self.shown.add(seg)
                    await self._send({"type": "subtitle", "seg": seg, "text": text, "final": False})

    async def run(self) -> None:
        tasks = [asyncio.create_task(job()) for job in (self._receive, self._tick, self._work)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                    await task


# ------------------------------------------------------------ endpoints


async def status_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(status())


async def tts_endpoint(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "JSON expected"}, status_code=400)
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "nothing to say"}, status_code=400)
    start()
    if voice.phase != "ready":
        return JSONResponse({"error": voice.message or "the voice isn't ready", **voice.status()}, status_code=503)
    try:
        wav = await voice.say(text, str(body.get("lang") or ""), str(body.get("voice") or ""), float(body.get("speed") or 1.0))
    except Exception as e:  # noqa: BLE001 — the browser falls back to its own voice
        logger.exception("TTS failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


async def stt_socket(ws: WebSocket) -> None:
    await ws.accept()
    start()
    await _Session(ws, str(ws.query_params.get("lang") or "")).run()


def register(app) -> None:  # noqa: ANN001 — a Reflex App
    routes = app._api.routes
    known = {getattr(r, "path", None) for r in routes}
    if STATUS_PATH not in known:
        routes.append(Route(STATUS_PATH, status_endpoint, methods=["GET"]))
    if TTS_PATH not in known:
        routes.append(Route(TTS_PATH, tts_endpoint, methods=["POST"]))
    if STT_PATH not in known:
        routes.append(WebSocketRoute(STT_PATH, stt_socket))
