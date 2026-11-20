"""Parse uploaded documents (PDF, DOCX, MD, TXT) into plain text with markdown-style headings."""

import io
import re
from dataclasses import dataclass
from pathlib import PurePath

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from core.ingest.errors import FileTooLarge, UnsupportedFile
from core.ingest.language import detect_language

MAX_FILE_BYTES = 20 * 1024 * 1024
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")

_HEADING_STYLE = re.compile(r"^(?:Heading|Otsikko)\s+(\d)$", re.IGNORECASE)
_MD_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedDoc:
    title: str
    text: str
    language: str | None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")  # cannot fail


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_pdf(data: bytes) -> tuple[str | None, str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnsupportedFile("PDF is password protected")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        meta_title = (reader.metadata.title if reader.metadata else None) or None
    except PyPdfError as e:
        raise UnsupportedFile(f"cannot read PDF: {e}") from e
    return (meta_title.strip() if meta_title else None), "\n\n".join(p for p in pages if p)


def _docx_blocks(doc) -> list[Paragraph | Table]:
    """Body paragraphs and tables in document order."""
    blocks: list[Paragraph | Table] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            blocks.append(Paragraph(child, doc))
        elif child.tag.endswith("}tbl"):
            blocks.append(Table(child, doc))
    return blocks


def _parse_docx(data: bytes) -> tuple[str | None, str]:
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:  # python-docx raises assorted errors (zip, xml, KeyError) on bad input
        raise UnsupportedFile(f"cannot read DOCX: {e}") from e

    lines: list[str] = []
    title: str | None = None
    for block in _docx_blocks(doc):
        if isinstance(block, Table):
            for row in block.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
            continue
        text = block.text.strip()
        if not text:
            lines.append("")
            continue
        style = block.style.name if block.style is not None and block.style.name else ""
        if style == "Title":
            title = title or text
            lines.append(f"# {text}")
        elif m := _HEADING_STYLE.match(style):
            level = min(max(int(m.group(1)), 1), 6)
            lines.append(f"{'#' * level} {text}")
        elif style.startswith(("List Bullet", "List Number", "List")):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return title, _clean("\n".join(lines))


def _fallback_title(filename: str) -> str:
    return PurePath(filename).stem.replace("_", " ").replace("-", " ").strip() or filename


def parse_file(filename: str, data: bytes) -> ParsedDoc:
    """Parse a document. Raises FileTooLarge / UnsupportedFile (domain errors)."""
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge(f"{filename}: {len(data)} bytes exceeds {MAX_FILE_BYTES}")
    ext = PurePath(filename).suffix.lower()

    title: str | None
    if ext == ".pdf":
        title, text = _parse_pdf(data)
    elif ext == ".docx":
        title, text = _parse_docx(data)
    elif ext in (".md", ".txt"):
        text = _clean(_decode(data))
        title = None
        if ext == ".md" and (m := _MD_HEADING.search(text)):
            title = m.group(1).strip()
    else:
        raise UnsupportedFile(f"{filename}: unsupported type {ext or '(none)'}")

    text = _clean(text)
    if not text:
        raise UnsupportedFile(f"{filename}: no extractable text (scanned PDF?)")
    if not title and ext == ".docx":
        m = _MD_HEADING.search(text)  # first heading doubles as title
        title = m.group(1).strip() if m else None
    return ParsedDoc(
        title=title or _fallback_title(filename), text=text, language=detect_language(text)
    )
