"""Turn an uploaded document into plain text for the detection box.

Deliberately stdlib-only for .txt/.md/.docx: a .docx is a zip of XML parts and the body
is recoverable with ElementTree alone, so Word support adds no dependency. Only PDF
needs a third-party library (pypdf, with PyMuPDF as fallback) — writing a real PDF
text extractor from scratch is not a thing to smuggle into a detection app.

The output feeds the SAME /detect path as pasted text, so nothing here is a security
boundary and every downstream length limit still applies. The one thing enforced here
is MAX_TEXT_CHARS, mirroring the UI's MAX_TEXT_LENGTH, so a 400-page PDF cannot bypass
the cap the workflow already validates against.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_TEXT_CHARS = 50_000  # same cap the Angular textarea and the workflow enforce
PDF_MAX_PAGES = 120      # CPU guard: we stop before extracting a novel nobody will paste

SUPPORTED = {".txt", ".md", ".markdown", ".text", ".docx", ".pdf"}

# Per-type upload ceilings, checked after werkzeug's global MAX_CONTENT_LENGTH.
PER_TYPE_BYTES = {
    ".txt": 5 * 1024 * 1024,
    ".md": 5 * 1024 * 1024,
    ".markdown": 5 * 1024 * 1024,
    ".text": 5 * 1024 * 1024,
    ".docx": 10 * 1024 * 1024,
    ".pdf": 20 * 1024 * 1024,
}


class ExtractError(Exception):
    """Raised for anything the user can act on; the message is UI-safe."""


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _plain_text(data: bytes) -> str:
    bom = data[:2]
    order = ["utf-16", "utf-8-sig", "utf-8", "cp1252"] if bom in (b"\xff\xfe", b"\xfe\xff") \
        else ["utf-8-sig", "utf-8", "cp1252"]
    for enc in order:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ExtractError("This file is not readable as text (unknown encoding).")


def _docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ExtractError("This .docx is not a valid Word file (corrupt or renamed).") from e
    try:
        xml = zf.read("word/document.xml")
    except KeyError as e:
        raise ExtractError("No document body found inside this .docx.") from e
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ExtractError("The Word document XML could not be parsed.") from e

    out: list[str] = []
    total = 0
    for p in root.iter(W + "p"):  # paragraphs, including the ones inside tables
        buf: list[str] = []
        for node in p.iter():
            if node.tag == W + "t":
                buf.append(node.text or "")
            elif node.tag == W + "tab":
                buf.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                buf.append("\n")
        line = "".join(buf)
        out.append(line)
        total += len(line)
        if total > MAX_TEXT_CHARS * 2:  # extracted plenty; stop for CPU, caller truncates
            break
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    last_err = ""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    raise ExtractError("This PDF is password-protected.") from None
            pages = []
            total = 0
            for i, pg in enumerate(reader.pages):
                if i >= PDF_MAX_PAGES:
                    break
                pages.append(pg.extract_text() or "")
                total = sum(len(x) for x in pages)
                if total > MAX_TEXT_CHARS * 2:
                    break
            return "\n".join(pages)
        except ExtractError:
            raise
        except Exception as e:  # malformed pdf etc — try the fallback before giving up
            last_err = str(e)
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ExtractError(
            "PDF support is not installed on this backend (pip install pypdf)."
        ) from None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise ExtractError("This PDF is password-protected.")
        pages = []
        total = 0
        for i, pg in enumerate(doc):
            if i >= PDF_MAX_PAGES:
                break
            pages.append(pg.get_text() or "")
            total = sum(len(x) for x in pages)
            if total > MAX_TEXT_CHARS * 2:
                break
        return "\n".join(pages)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(f"This PDF could not be parsed ({last_err or e}).") from e


def extract_text(ext: str, data: bytes) -> str:
    """ext is the lower-cased suffix; raises ExtractError with a UI-safe message."""
    if ext in (".txt", ".md", ".markdown", ".text"):
        text = _plain_text(data)
    elif ext == ".docx":
        text = _docx_text(data)
    elif ext == ".pdf":
        text = _pdf_text(data)
    else:
        raise ExtractError("Unsupported file type.")

    text = text.replace("\x00", "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
