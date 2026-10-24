"""Attached files: kept as they arrive, and what an AI is shown of each kind (real pictures, a real
video made with the bundled ffmpeg, a real PDF, and Office files built here)."""

from __future__ import annotations

import base64
import io
import subprocess
import time
import zipfile

import pytest
from PIL import Image

from ai_hub import attachments


def keep(name: str, data: bytes, mime: str = "", piece: int = 4096) -> attachments.Attachment:
    """A file as an upload brings it: in pieces."""
    incoming = attachments.Incoming(name, mime)
    for i in range(0, len(data), piece):
        incoming.write(data[i : i + piece])
    return incoming.finish()


def png_bytes(size: tuple[int, int] = (40, 30), color: str = "red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def pdf_bytes(pages: list[str]) -> bytes:
    """A small PDF with a line of text on each page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def video_bytes(tmp_path, seconds: int = 3) -> bytes:
    """A test-pattern video with a tone, made with the bundled ffmpeg."""
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [attachments._ffmpeg(), "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(out)],
        check=True,
    )
    return out.read_bytes()


def office_bytes(parts: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return out.getvalue()


def picture_of(part: attachments.Part) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(part.image[1])))


def test_a_picture_is_shown_upright_and_no_larger_than_claude_takes() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # a phone held upright: stored sideways, to be turned a quarter
    out = io.BytesIO()
    Image.new("RGB", (3000, 2000), "blue").save(out, "JPEG", exif=exif)

    att = keep("IMG_0001.JPG", out.getvalue(), "image/jpeg")

    assert att.kind == "image" and att.what == "a picture, 2000×3000"
    assert att.thumb.startswith("data:image/jpeg;base64,")
    assert attachments.get(att.id) == att
    about, shown = attachments.view(att)
    assert about.text == f"IMG_0001.JPG (a picture, 2000×3000, {attachments._size(att.size)}):"
    picture = picture_of(shown)
    assert picture.height == attachments.MAX_SIDE and picture.width < picture.height


def test_see_through_pictures_stay_png() -> None:
    out = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(out, "PNG")

    _, shown = attachments.view(keep("logo.png", out.getvalue(), "image/png"))

    assert shown.image[0] == "image/png"


def test_a_video_is_shown_as_frames_with_their_times(tmp_path) -> None:
    att = keep("clip.mp4", video_bytes(tmp_path), "video/mp4", piece=65536)

    assert att.kind == "video" and att.what == "a video, 0:03, 320×240, with sound" and att.thumb

    shown = attachments.view(att)
    pictures = [p for p in shown if p.image]
    assert len(pictures) == attachments.FRAMES and "8 frames from 0:00.0 to 0:03.0" in shown[0].text
    assert "sound isn't included" in shown[0].text
    assert [p.text for p in shown[1:3]] == ["0:00.1", ""]  # each frame after its time
    assert picture_of(pictures[0]).size == (320, 240)

    part = attachments.view(att, span="1-2")
    assert "4 frames from 0:01.0 to 0:02.0" in part[0].text and len([p for p in part if p.image]) == 4


def test_a_pdf_is_shown_page_by_page() -> None:
    att = keep("plan.pdf", pdf_bytes([f"Floor {n}" for n in range(1, 7)]))

    assert att.kind == "pdf" and att.pages == 6 and att.what == "a PDF, 6 pages" and att.thumb

    first = attachments.view(att)
    assert "Pages 1–4 of 6" in first[0].text
    assert [p.text for p in first if p.text.startswith("Page ")] == [f"Page {n}:\nFloor {n}" for n in range(1, 5)]
    assert len([p for p in first if p.image]) == 4
    rest = attachments.view(att, pages="5-8")
    assert "Pages 5–6 of 6" in rest[0].text and len([p for p in rest if p.image]) == 2
    assert "Pages 2–2" in attachments.view(att, pages="2")[0].text

    assert attachments.parts([att], pdf=True)[1].pdf == base64.b64encode(att.path.read_bytes()).decode()
    assert not any(p.pdf for p in attachments.parts([att]))


def test_text_comes_a_part_at_a_time() -> None:
    text = "".join(f"line {n}\n" for n in range(10_000))
    att = keep("log.txt", text.encode(), "text/plain")

    assert att.kind == "text" and att.what == "a text file, 10,001 lines"
    about, piece, more = attachments.view(att)
    assert piece.text == text[: attachments.TEXT_CHARS]
    assert f"from offset {attachments.TEXT_CHARS}" in more.text
    _, rest, *more = attachments.view(att, offset=attachments.TEXT_CHARS * 2)
    assert rest.text == text[attachments.TEXT_CHARS * 2 :] and not more


def test_code_and_big5_text_are_text_whatever_the_browser_called_them() -> None:
    assert keep("part.step", b"ISO-10303-21;\nHEADER;\n", "").kind == "text"
    assert keep("main.ts", b"export const x = 1\n", "video/mp2t").kind == "text"
    att = keep("old.csv", "名稱,數量\n椅子,4\n".encode("cp950"), "text/csv")
    assert attachments.view(att)[1].text == "名稱,數量\n椅子,4\n"


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def test_word_excel_and_powerpoint_files_are_read_as_text() -> None:
    word = office_bytes({
        "word/document.xml": f"<w:document {W}><w:body>"
        "<w:p><w:r><w:t>Chapel</w:t></w:r><w:r><w:tab/><w:t>12 m</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nave</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>45 m</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>",
    })
    excel = office_bytes({
        "xl/workbook.xml": f'<workbook {S}><sheets><sheet name="Sizes" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships {RELS}><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Tower</t></si></sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row r="2"><c r="A2" t="s"><v>0</v></c><c r="C2"><v>172.5</v></c></row></sheetData></worksheet>',
    })
    slide = '<p:sld {P}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    deck = office_bytes({
        "ppt/presentation.xml": f'<p:presentation {P}><p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships {RELS}><Relationship Id="rId1" Target="slides/slide1.xml"/><Relationship Id="rId2" Target="slides/slide2.xml"/></Relationships>',
        "ppt/slides/slide1.xml": slide.replace("{P}", P).replace("{}", "Second"),
        "ppt/slides/slide2.xml": slide.replace("{P}", P).replace("{}", "First"),
    })

    texts = {name: attachments.view(keep(name, data))[1].text for name, data in
             [("brief.docx", word), ("sizes.xlsx", excel), ("deck.pptx", deck)]}

    assert texts["brief.docx"] == "Chapel\t12 m\nNave | 45 m"
    assert texts["sizes.xlsx"] == "# Sheet: Sizes\n2\tTower\t\t172.5"
    assert texts["deck.pptx"] == "# Slide 1\nFirst\n# Slide 2\nSecond"  # the deck's order, not the files'
    assert keep("brief.docx", word).what == "a Word document"


def test_other_files_say_what_they_are_and_damaged_ones_say_so() -> None:
    other = attachments.view(keep("model.bin", bytes(range(256)) * 4, "application/octet-stream"))
    assert len(other) == 1 and "can't show this kind of file" in other[0].text

    broken = keep("photo.jpg", b"\xff\xd8\xff not really a jpeg", "image/jpeg")
    assert broken.kind == "image" and broken.what == "a picture"
    assert "could not open it" in attachments.view(broken)[0].text


def test_too_large_a_file_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(attachments, "MAX_BYTES", 10)
    incoming = attachments.Incoming("big.bin", "")
    with pytest.raises(ValueError, match="larger than"):
        incoming.write(b"x" * 11)
    incoming.discard()
    assert not any(attachments._root().iterdir())


def test_files_are_kept_a_week() -> None:
    att = keep("red.png", png_bytes(), "image/png")
    attachments.sweep(now=time.time() + (attachments.KEEP_DAYS - 1) * 86400)
    assert attachments.get(att.id) is not None
    attachments.sweep(now=time.time() + (attachments.KEEP_DAYS + 1) * 86400)
    assert attachments.get(att.id) is None and not att.path.exists()


def test_ids_are_checked_before_they_touch_the_disk() -> None:
    assert attachments.get("../../settings") is None
    attachments.remove("../..")  # nothing happens
    assert attachments.get("") is None


def test_views_become_mcp_content() -> None:
    shown = attachments.view(keep("red.png", png_bytes(), "image/png"))
    content = attachments.mcp_content(shown)
    assert content[0]["type"] == "text" and content[1] == {"type": "image", "mimeType": "image/jpeg", "data": shown[1].image[1]}
    assert attachments.summary(shown) == "a picture"
