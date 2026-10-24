"""Files people give the assistant, and what an AI is shown of them.

A file dropped, pasted or picked in the assistant is kept in ``<data>/attachments/<id>/``
(``config.data_dir``) for KEEP_DAYS, and goes with the next message. Models take text and
pictures, so ``view`` turns each file into those:

- a picture: itself, at most MAX_SIDE pixels on its long side, turned upright;
- a video: how long it is, and FRAMES frames across it (or across a span of it) with their times;
- a PDF: each page's text and a picture of the page, PAGES pages at a time;
- text, code and data (CSV, JSON, SVG, STEP, …): the text, TEXT_CHARS characters at a time;
- Word, Excel and PowerPoint files: their text, in the order of the pages, sheets and slides;
- anything else, sound included: its name, type and size.

The assistant's models get that with the message (``parts``; Claude takes a PDF as it is). The
Claude Code on the owner's computer gets a list of the files and opens each with its channel's
``attachment`` tool (``ai_hub.session``), a range of pages or a span of a video at a time.
"""

from __future__ import annotations

import base64
import codecs
import concurrent.futures
import dataclasses
import io
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from PIL import Image, ImageOps

from ai_hub import config

KEEP_DAYS = 7
MAX_BYTES = int(os.environ.get("AI_HUB_MAX_ATTACHMENT_MB") or 2048) * 1024 * 1024
MAX_SIDE = 1568  # Claude scales longer pictures down itself
FRAME_SIDE = 1024
THUMB_SIDE = 160
FRAMES = 8
PAGES = 4
TEXT_CHARS = 40_000  # Claude Code keeps a longer tool result in a file, which the session can't read
PDF_BYTES = 32 * 1024 * 1024  # the largest PDF Claude takes as it is
PDF_PAGES = 100
READ_BYTES = 64 * 1024 * 1024  # of a text file, or of one part of an Office file
TIMEOUT = 60.0  # for one ffmpeg run

_VIDEO = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
_AUDIO = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aif", ".aiff"}
_PICTURE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
_OFFICE = {".docx": "a Word document", ".xlsx": "an Excel workbook", ".pptx": "a PowerPoint deck"}
_ID = re.compile(r"[0-9a-f]{16}")
# pdfium may be used by one thread at a time; views run in threads.
_pdfium = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Part:
    """A piece of what a model is shown: text, a picture, or a PDF as it is (for Claude)."""

    text: str = ""
    image: tuple[str, str] | None = None  # (mime, base64)
    pdf: str = ""  # base64


@dataclasses.dataclass
class Attachment:
    id: str
    name: str
    mime: str
    size: int
    kind: str  # image | video | audio | pdf | text | document | other
    created: float
    what: str = ""  # e.g. "a picture, 4032×3024", "a video, 1:30, 1920×1080, with sound"
    pages: int = 0  # a PDF's
    thumb: str = ""  # a small picture of it for the chat (a data: URL), when it has one
    session: bool = False  # given to the Claude Code on the owner's computer

    @property
    def path(self) -> Path:
        return _root() / self.id / "file"

    def details(self) -> str:
        """e.g. "a picture, 4032×3024, 2.1 MB"."""
        return f"{self.what or 'a file'}, {_size(self.size)}"

    def about(self) -> str:
        """e.g. "photo.jpg (a picture, 4032×3024, 2.1 MB)"."""
        return f"{self.name} ({self.details()})"


# ------------------------------------------------------------ keeping them


def _root() -> Path:
    return config.data_dir() / "attachments"


def _save(att: Attachment) -> None:
    (_root() / att.id / "meta.json").write_text(json.dumps(dataclasses.asdict(att), ensure_ascii=False), encoding="utf-8")


def get(file_id: str) -> Attachment | None:
    if not _ID.fullmatch(file_id or ""):
        return None
    try:
        return Attachment(**json.loads((_root() / file_id / "meta.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def remove(file_id: str) -> None:
    if _ID.fullmatch(file_id or ""):
        shutil.rmtree(_root() / file_id, ignore_errors=True)


def give_to_session(files: Iterable[Attachment]) -> None:
    """Lets the Claude Code on the owner's computer open these (it gets only its messages' files)."""
    for att in files:
        if not att.session:
            att.session = True
            _save(att)


def sweep(now: float | None = None) -> None:
    """Removes the files kept longer than KEEP_DAYS."""
    root = _root()
    if not root.is_dir():
        return
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    for folder in root.iterdir():
        att = get(folder.name)
        try:
            created = att.created if att else folder.stat().st_mtime
        except OSError:
            continue
        if created < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


class Incoming:
    """A file arriving in pieces (an upload): ``write`` them, then ``finish``, or ``discard``."""

    def __init__(self, name: str, mime: str) -> None:
        sweep()
        self.id = secrets.token_hex(8)
        self.name = _clean(name)
        self.mime = mime
        self.size = 0
        folder = _root() / self.id
        folder.mkdir(parents=True)
        self._file = open(folder / "file", "wb")

    def write(self, data: bytes) -> None:
        self.size += len(data)
        if self.size > MAX_BYTES:
            raise ValueError(f"{self.name} is larger than {_size(MAX_BYTES)}")
        self._file.write(data)

    def finish(self) -> Attachment:
        """Keeps the file, with what it is. It is opened for that, so run this in a thread."""
        self._file.close()
        path = _root() / self.id / "file"
        with open(path, "rb") as f:
            head = f.read(8192)
        mime = self.mime if self.mime not in ("", "application/octet-stream") else (mimetypes.guess_type(self.name)[0] or "application/octet-stream")
        att = Attachment(self.id, self.name, mime, self.size, _kind(self.name, mime, head), time.time())
        try:
            att.what, att.pages, att.thumb = _describe(att)
        except Exception:  # noqa: BLE001 — damaged, or not what its name says: its view says so
            att.what = _KIND_WHAT[att.kind]
        _save(att)
        return att

    def discard(self) -> None:
        self._file.close()
        remove(self.id)


def _clean(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name).replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:200] or "file"


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" or n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_KIND_WHAT = {
    "image": "a picture",
    "video": "a video",
    "audio": "a sound file",
    "pdf": "a PDF",
    "text": "a text file",
    "document": "a document",
    "other": "a file",
}


def _kind(name: str, mime: str, head: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf" or mime == "application/pdf" or head.startswith(b"%PDF-"):
        return "pdf"
    if ext in _OFFICE:
        return "document"
    if ext in _PICTURE:
        return "image"
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    if _decode(head, final=False) is not None:  # code, data, SVG, …, whatever the browser called it
        return "text"
    for kind in ("image", "video", "audio"):
        if mime.startswith(kind + "/"):
            return kind
    return "other"


def _describe(att: Attachment) -> tuple[str, int, str]:
    """(what it is, its pages, a thumbnail) for a newly kept file."""
    if att.kind == "image":
        picture = _picture(att.path)
        return f"a picture, {picture.width}×{picture.height}", 0, _thumb(picture)
    if att.kind in ("video", "audio"):
        probe = _probe(att.path)
        if not probe.width:
            return f"a sound file, {_clock(probe.duration)}", 0, ""
        frame = _frame(att.path, min(1.0, probe.duration / 10), THUMB_SIDE)
        sound = "with sound" if probe.audio else "no sound"
        thumb = f"data:{frame[0]};base64,{frame[1]}" if frame else ""
        return f"a video, {_clock(probe.duration)}, {probe.width}×{probe.height}, {sound}", 0, thumb
    if att.kind == "pdf":
        import pypdfium2 as pdfium

        with _pdfium:
            pdf = pdfium.PdfDocument(str(att.path))
            try:
                pages = len(pdf)
                thumb = _thumb(_render(pdf[0], THUMB_SIDE)) if pages else ""
            finally:
                pdf.close()
        return f"a PDF, {pages} page" + ("" if pages == 1 else "s"), pages, thumb
    if att.kind == "document":
        return _OFFICE[Path(att.name).suffix.lower()], 0, ""
    if att.kind == "text":
        lines = (_read_text(att.path) or "").count("\n") + 1
        return f"a text file, {lines:,} line" + ("" if lines == 1 else "s"), 0, ""
    return f"a file of type {att.mime}", 0, ""


# ------------------------------------------------------------ what an AI is shown


def view(att: Attachment, pages: str = "", span: str = "", offset: int = 0) -> list[Part]:
    """What an AI is shown of the file: a line saying what it is and which part this is, then
    that part (``pages`` of a PDF such as "5-8", ``span`` of a video such as "1:30-2:00",
    text from character ``offset``)."""
    try:
        if att.kind == "image":
            return [Part(f"{att.about()}:"), Part(image=_encode(_picture(att.path), MAX_SIDE))]
        if att.kind == "video":
            return _video(att, span)
        if att.kind == "pdf":
            return _pdf(att, pages)
        if att.kind in ("text", "document"):
            return _text(att, offset)
    except Exception as e:  # noqa: BLE001 — damaged, or a format the libraries can't read
        return [Part(f"{att.about()}: the hub could not open it ({type(e).__name__}: {e}).")]
    if att.kind == "audio":
        return [Part(f"{att.about()}: the hub can't play sound to an AI, so only its name and length are known.")]
    return [Part(f"{att.about()}: the hub can't show this kind of file to an AI, so only its name, type and size are known.")]


def parts(files: Iterable[Attachment], pdf: bool = False) -> list[Part]:
    """What a model is shown of the files that go with a message (``pdf``: it reads PDFs as they
    are, up to PDF_BYTES and PDF_PAGES; others get their first pages)."""
    out: list[Part] = []
    for att in files:
        if pdf and att.kind == "pdf" and att.size <= PDF_BYTES and 0 < att.pages <= PDF_PAGES:
            out += [Part(f"{att.about()}:"), Part(pdf=base64.b64encode(att.path.read_bytes()).decode("ascii"))]
        else:
            out += view(att)
    return out


def mcp_content(parts: Iterable[Part]) -> list[dict[str, Any]]:
    """Parts as MCP tool result content (for the Claude Code on the owner's computer)."""
    out: list[dict[str, Any]] = []
    for p in parts:
        if p.image is not None:
            out.append({"type": "image", "mimeType": p.image[0], "data": p.image[1]})
        elif p.text:
            out.append({"type": "text", "text": p.text})
    return out


def summary(parts: Iterable[Part]) -> str:
    """What a view held, for the chat: e.g. "8 pictures and text"."""
    parts = list(parts)
    pictures = sum(1 for p in parts if p.image is not None)
    said = "a picture" if pictures == 1 else f"{pictures} pictures" if pictures else ""
    texts = len([p for p in parts if p.text]) > 1  # more than the line saying what it is
    return " and ".join(x for x in (said, "text" if texts else "") if x) or "its name and size"


# ------------------------------------------------------------ pictures


def _picture(path: Path) -> Image.Image:
    """The picture, upright; formats Pillow lacks (iPhone HEIC, …) go through ffmpeg."""
    try:
        picture = Image.open(path)
        picture.load()
    except (OSError, SyntaxError):
        png = _ffmpeg_png(path, 0.0, 0, seek=False)
        if png is None:
            raise
        picture = Image.open(io.BytesIO(png))
    return ImageOps.exif_transpose(picture)


def _encode(picture: Image.Image, side: int) -> tuple[str, str]:
    """(mime, base64): a JPEG at most ``side`` pixels on its long side, or a PNG when the
    picture has see-through parts."""
    picture = picture.copy()
    picture.thumbnail((side, side), Image.Resampling.LANCZOS)
    alpha = picture.mode in ("RGBA", "LA", "PA") or (picture.mode == "P" and "transparency" in picture.info)
    out = io.BytesIO()
    if alpha:
        picture.convert("RGBA").save(out, "PNG", optimize=True)
        mime = "image/png"
    else:
        picture.convert("RGB").save(out, "JPEG", quality=85)
        mime = "image/jpeg"
    return mime, base64.b64encode(out.getvalue()).decode("ascii")


def _thumb(picture: Image.Image) -> str:
    mime, data = _encode(picture, THUMB_SIDE)
    return f"data:{mime};base64,{data}"


# ------------------------------------------------------------ videos (ffmpeg, from imageio-ffmpeg)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@dataclasses.dataclass
class _Probe:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    audio: bool = False


def _probe(path: Path) -> _Probe:
    """Length, picture size (as shown, turned) and sound, from what ffmpeg says of the file."""
    said = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, timeout=TIMEOUT).stderr.decode("utf-8", "replace")
    probe = _Probe(audio=bool(re.search(r"Stream #.*: Audio:", said)))
    if m := re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", said):
        probe.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := re.search(r"Stream #.*: Video: .*?\b(\d{2,5})x(\d{2,5})\b", said):
        probe.width, probe.height = int(m[1]), int(m[2])
        turn = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", said)
        if turn and round(abs(float(turn[1]))) % 180 == 90:
            probe.width, probe.height = probe.height, probe.width
    return probe


def _ffmpeg_png(path: Path, at: float, side: int, seek: bool = True) -> bytes | None:
    """One frame as a PNG (``at`` seconds in), at most ``side`` pixels on its long side (0: as is)."""
    command = [_ffmpeg(), "-v", "error"] + (["-ss", f"{at:.3f}"] if seek else []) + ["-i", str(path), "-frames:v", "1", "-an", "-sn"]
    if side:
        command += ["-vf", f"scale='min(iw,{side})':'min(ih,{side})':force_original_aspect_ratio=decrease"]
    result = subprocess.run(command + ["-f", "image2pipe", "-c:v", "png", "pipe:1"], capture_output=True, timeout=TIMEOUT)
    return result.stdout if result.returncode == 0 and result.stdout else None


def _frame(path: Path, at: float, side: int) -> tuple[str, str] | None:
    png = _ffmpeg_png(path, at, side)
    return _encode(Image.open(io.BytesIO(png)), side) if png else None


def _clock(seconds: float, tenths: bool = False) -> str:
    """1:05, 1:02:03, or 0:01.5 with ``tenths``."""
    whole = int(seconds)
    h, m, s = whole // 3600, whole // 60 % 60, whole % 60
    text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return text + f".{int((seconds - whole) * 10)}" if tenths else text


def _seconds(text: str) -> float:
    total = 0.0
    for piece in text.strip().split(":"):
        total = total * 60 + float(piece)
    return total


def _span(text: str, duration: float) -> tuple[float, float]:
    """A span such as "30-60" or "1:30-2:00" (seconds, m:ss or h:mm:ss) within the video; the
    whole video when there is none."""
    start, sep, end = (text or "").partition("-")
    try:
        a = _seconds(start) if start.strip() else 0.0
        b = _seconds(end) if sep and end.strip() else duration
    except ValueError:
        return 0.0, duration
    a, b = max(0.0, min(a, duration)), max(0.0, min(b, duration))
    return (a, b) if b > a else (0.0, duration)


def _video(att: Attachment, span: str) -> list[Part]:
    probe = _probe(att.path)
    if not probe.width:
        return [Part(f"{att.about()}: it has no picture, and the hub can't play sound to an AI.")]
    start, end = _span(span, probe.duration)
    count = max(1, min(FRAMES, math.ceil((end - start) * 4)))
    times = [start + (end - start) * (i + 0.5) / count for i in range(count)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:  # each ffmpeg decodes a frame
        frames = list(pool.map(lambda t: _frame(att.path, t, FRAME_SIDE), times))
    tenths = end - start < 60
    sound = " Its sound isn't included: the hub can't play sound to an AI." if probe.audio else ""
    out = [Part(f"{att.about()}. {count} frames from {_clock(start, tenths)} to {_clock(end, tenths)}, each after its time.{sound}")]
    for t, frame in zip(times, frames):
        out.append(Part(_clock(t, tenths) + ("" if frame else " (this frame could not be read)")))
        if frame:
            out.append(Part(image=frame))
    return out


# ------------------------------------------------------------ PDFs (pdfium)


def _render(page: Any, side: int) -> Image.Image:
    width, height = page.get_size()
    return page.render(scale=min(side / max(width, height, 1), 4)).to_pil()


def _pages(text: str, count: int) -> tuple[int, int]:
    """Pages such as "5-8", "5" or "5-" within the document, at most PAGES of them."""
    first, sep, last = (text or "").partition("-")
    try:
        a = int(first) if first.strip() else 1
        b = int(last) if last.strip() else (a + PAGES - 1 if sep or not first.strip() else a)
    except ValueError:
        a, b = 1, PAGES
    a = max(1, min(a, count))
    return a, max(a, min(b, a + PAGES - 1, count))


def _pdf(att: Attachment, pages: str) -> list[Part]:
    import pypdfium2 as pdfium

    with _pdfium:
        pdf = pdfium.PdfDocument(str(att.path))
        try:
            count = len(pdf)
            if not count:
                return [Part(f"{att.about()}: it has no pages.")]
            first, last = _pages(pages, count)
            out = [Part(f"{att.about()}. Pages {first}–{last} of {count}: each page's text, then a picture of the page.")]
            for n in range(first, last + 1):
                page = pdf[n - 1]
                text = page.get_textpage().get_text_bounded().strip()[: TEXT_CHARS // PAGES]
                out += [Part(f"Page {n}:\n{text or '(no text: see the picture)'}"), Part(image=_encode(_render(page, MAX_SIDE), MAX_SIDE))]
            return out
        finally:
            pdf.close()


# ------------------------------------------------------------ text, and Office files


def _decode(data: bytes, final: bool = True) -> str | None:
    """The text, from UTF-8 (UTF-16 with its mark, or Big5, as Taiwan's older files often are);
    None when it isn't text."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data:
        return None
    for encoding in ("utf-8-sig", "cp950"):
        try:
            text = codecs.getincrementaldecoder(encoding)().decode(data, final=final)
        except UnicodeDecodeError:
            continue
        controls = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r\f\v\x1b")
        return text if controls <= len(text) // 100 else None
    return None


def _read_text(path: Path) -> str | None:
    with open(path, "rb") as f:
        return _decode(f.read(READ_BYTES))


def _text(att: Attachment, offset: int) -> list[Part]:
    text = _office(att.path) if att.kind == "document" else _read_text(att.path)
    if text is None:
        return [Part(f"{att.about()}: it isn't text the hub can read.")]
    offset = max(0, min(int(offset or 0), len(text)))
    piece = text[offset : offset + TEXT_CHARS]
    end = offset + len(piece)
    where = "the whole text" if offset == 0 and end == len(text) else f"characters {offset:,} to {end:,} of {len(text):,}"
    out = [Part(f"{att.about()}, {where}:"), Part(piece or "(empty)")]
    if end < len(text):
        out.append(Part(f"(The text goes on: {len(text) - end:,} more characters from offset {end}.)"))
    return out


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _xml(z: zipfile.ZipFile, name: str) -> ElementTree.Element:
    if z.getinfo(name).file_size > READ_BYTES:
        raise ValueError(f"{name} is too large")
    return ElementTree.fromstring(z.read(name))


def _targets(z: zipfile.ZipFile, rels: str, folder: str) -> dict[str, str]:
    """A part's relationships: id → the path of the part it points to, in the zip."""
    out = {}
    for rel in _xml(z, rels):
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = target.lstrip("/") if target.startswith("/") else f"{folder}/{target}"
    return out


def _office(path: Path) -> str:
    """The text of a Word, Excel or PowerPoint file."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "word/document.xml" in names:
            return _word(z)
        if "xl/workbook.xml" in names:
            return _excel(z, names)
        if "ppt/presentation.xml" in names:
            return _powerpoint(z)
    raise ValueError("not a Word, Excel or PowerPoint file")


def _paragraph(p: ElementTree.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{_W}t":
            out.append(el.text or "")
        elif el.tag == f"{_W}tab":
            out.append("\t")
        elif el.tag in (f"{_W}br", f"{_W}cr"):
            out.append("\n")
    return "".join(out)


def _word(z: zipfile.ZipFile) -> str:
    body = _xml(z, "word/document.xml").find(f"{_W}body")
    lines = []
    for el in body if body is not None else []:
        if el.tag == f"{_W}tbl":  # a table: a row per line, its cells apart
            for row in el.iter(f"{_W}tr"):
                cells = [" ".join(_paragraph(p) for p in cell.iter(f"{_W}p")).strip() for cell in row.findall(f"{_W}tc")]
                lines.append(" | ".join(cells))
        else:
            lines += [_paragraph(p) for p in ([el] if el.tag == f"{_W}p" else el.iter(f"{_W}p"))]
    return "\n".join(lines).strip()


def _column(ref: str) -> int:
    """A cell reference's column, from 0: "C7" → 2."""
    n = 0
    for c in re.match(r"[A-Z]*", ref.upper())[0]:
        n = n * 26 + ord(c) - 64
    return n - 1


def _excel(z: zipfile.ZipFile, names: set[str]) -> str:
    shared = []
    if "xl/sharedStrings.xml" in names:
        shared = ["".join(t.text or "" for t in si.iter(f"{_S}t")) for si in _xml(z, "xl/sharedStrings.xml").iter(f"{_S}si")]
    targets = _targets(z, "xl/_rels/workbook.xml.rels", "xl")
    lines = []
    for sheet in _xml(z, "xl/workbook.xml").iter(f"{_S}sheet"):
        lines.append(f"# Sheet: {sheet.get('name')}")
        part = targets.get(sheet.get(f"{_R}id", ""), "")
        if part not in names:
            continue
        for row in _xml(z, part).iter(f"{_S}row"):
            cells: dict[int, str] = {}
            for i, c in enumerate(row.findall(f"{_S}c")):
                kind, value = c.get("t"), c.findtext(f"{_S}v") or ""
                if kind == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{_S}t"))
                elif kind == "b":
                    value = "TRUE" if value == "1" else "FALSE"
                if value:
                    cells[_column(c.get("r", "")) if c.get("r") else i] = value
            if cells:  # the row's number, then its cells from column A, tab-separated
                lines.append("\t".join([row.get("r", "")] + [cells.get(i, "") for i in range(max(cells) + 1)]))
    return "\n".join(lines)


def _powerpoint(z: zipfile.ZipFile) -> str:
    targets = _targets(z, "ppt/_rels/presentation.xml.rels", "ppt")
    lines = []
    for n, slide in enumerate(_xml(z, "ppt/presentation.xml").iter(f"{_P}sldId"), 1):
        lines.append(f"# Slide {n}")
        part = targets.get(slide.get(f"{_R}id", ""), "")
        if part in z.namelist():
            texts = ("".join(t.text or "" for t in p.iter(f"{_A}t")) for p in _xml(z, part).iter(f"{_A}p"))
            lines += [t for t in texts if t.strip()]
    return "\n".join(lines)
