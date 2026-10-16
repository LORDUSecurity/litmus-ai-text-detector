"""POST /extract tests. Run from ai-text-detector/:  python -m pytest backend/tests -q

Fixtures build real files in memory: the .docx is a hand-made zip of word/document.xml
(the same thing the extractor reads), and the .pdf is generated with PyMuPDF when present.
"""
import io
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-test-")
os.environ.setdefault("DETECTOR_DB", os.path.join(_TMP, "test.sqlite3"))
os.environ.setdefault("DETECTOR_SECRET", os.path.join(_TMP, "test-secret.key"))
os.environ.setdefault("N8N_DB", os.path.join(_TMP, "no-such-n8n.sqlite"))

import zipfile  # noqa: E402

import pytest  # noqa: E402

from backend import db  # noqa: E402
from backend.app import create_app  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@pytest.fixture()
def client():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def upload(client, data: bytes, filename: str):
    return client.post(
        "/extract",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def _docx(paragraphs) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs
    )
    xml = f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_plain_txt_round_trip(client):
    r = upload(client, b"hello\r\nworld\r\n", "note.txt")
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["ok"] is True and j["text"] == "hello\nworld" and j["chars"] == 11
    assert j["kind"] == ".txt" and j["truncated"] is False


def test_markdown_alias_and_null_stripping(client):
    r = upload(client, b"# Title\x00\n\nsome text here", "readme.md")
    assert r.status_code == 200
    assert r.get_json()["text"] == "# Title\n\nsome text here"


def test_docx_extracts_paragraphs(client):
    r = upload(client, _docx(["First paragraph", "Second one"]), "essay.docx")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["text"] == "First paragraph\nSecond one"


def test_pdf_text_layer(client):
    fitz = pytest.importorskip("fitz")
    d = fitz.open()
    pg = d.new_page()
    pg.insert_text((72, 72), "A real pdf line with enough letters.", fontsize=11)
    pdf = d.tobytes()
    r = upload(client, pdf, "paper.pdf")
    assert r.status_code == 200, r.get_json()
    assert "A real pdf line" in r.get_json()["text"]


def test_unsupported_extension_is_415_with_the_allowed_list(client):
    r = upload(client, b"MZ", "virus.exe")
    assert r.status_code == 415
    j = r.get_json()
    assert j["error_code"] == "UNSUPPORTED_TYPE"
    assert ".pdf" in j["allowed"] and ".docx" in j["allowed"]


def test_missing_multipart_field_is_400(client):
    r = client.post("/extract", data={"other": (io.BytesIO(b"hi there friend"), "a.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "NO_FILE"


def test_empty_file_is_400(client):
    r = upload(client, b"", "empty.txt")
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "EMPTY_FILE"


def test_no_text_in_a_pdf_like_file_is_422_extract_empty(client):
    # 3 bytes decodes fine but is below the 10-char detect minimum
    r = upload(client, b"abc", "tiny.txt")
    assert r.status_code == 422
    assert r.get_json()["error_code"] == "EXTRACT_EMPTY"


def test_corrupt_docx_fails_cleanly(client):
    r = upload(client, b"this is not a zip archive at all", "broken.docx")
    assert r.status_code == 422
    j = r.get_json()
    assert j["error_code"] == "EXTRACT_FAILED"
    assert "valid Word file" in j["error"]


def test_per_type_size_cap(client):
    from backend import extract

    big = b"x" * (extract.PER_TYPE_BYTES[".txt"] + 1)
    r = upload(client, big, "huge.txt")
    assert r.status_code == 413
    assert r.get_json()["error_code"] == "FILE_TOO_LARGE"


def test_extracted_text_is_capped_at_detect_limit(client):
    from backend import extract

    body = ("word " * 20000)  # ~120k chars
    r = upload(client, body.encode(), "long.txt")
    assert r.status_code == 200
    j = r.get_json()
    assert j["chars"] == extract.MAX_TEXT_CHARS == 50_000
    assert j["truncated"] is True
