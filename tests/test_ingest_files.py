import io

import pytest
from docx import Document

from core.ingest.errors import FileTooLarge, UnsupportedFile
from core.ingest.files import MAX_FILE_BYTES, parse_file

FI_TEXT = (
    "Meidän palvelut sopivat pienille yrityksille, ja voit ottaa yhteyttä meille milloin vain."
)


def make_pdf(text: str | None, title: str | None = None) -> bytes:
    """Hand-built minimal one-page PDF (no extra dependency); text=None makes a blank page."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if title:
        objs.append(f"<< /Title ({title}) >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    info = f" /Info {len(objs)} 0 R" if title else ""
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R{info} >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(out)


def make_docx() -> bytes:
    doc = Document()
    doc.add_heading("Hinnasto", level=1)
    doc.add_paragraph(FI_TEXT)
    doc.add_heading("Paketit", level=2)
    doc.add_paragraph("Perus", style="List Bullet")
    doc.add_paragraph("Pro", style="List Bullet")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Paketti"
    table.cell(0, 1).text = "Hinta"
    table.cell(1, 0).text = "Perus"
    table.cell(1, 1).text = "990 €"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_markdown_title_from_first_heading() -> None:
    doc = parse_file("opas.md", f"# Käyttöopas\n\n{FI_TEXT}\n".encode())
    assert doc.title == "Käyttöopas"
    assert doc.text.startswith("# Käyttöopas")
    assert doc.language == "fi"


def test_txt_title_falls_back_to_filename() -> None:
    doc = parse_file(
        "hinta_lista-2026.txt", b"The price list is in the document and you can contact us."
    )
    assert doc.title == "hinta lista 2026"
    assert doc.language == "en"


def test_text_decoding_fallbacks() -> None:
    assert "Ääni" in parse_file("a.txt", "﻿Ääni on tärkeä".encode()).text
    assert "tärkeä" in parse_file("b.txt", "tärkeä".encode("cp1252")).text  # not valid utf-8


def test_docx_headings_lists_and_tables() -> None:
    doc = parse_file("hinnasto.docx", make_docx())
    lines = doc.text.splitlines()
    assert "# Hinnasto" in lines
    assert "## Paketit" in lines
    assert "- Perus" in lines and "- Pro" in lines
    assert "Perus | 990 €" in lines
    assert doc.title == "Hinnasto"
    assert doc.language == "fi"


def test_pdf_text_and_metadata_title() -> None:
    doc = parse_file("x.pdf", make_pdf("Hello from the PDF", title="Brochure"))
    assert "Hello from the PDF" in doc.text
    assert doc.title == "Brochure"


def test_pdf_title_falls_back_to_filename() -> None:
    assert parse_file("esite-2026.pdf", make_pdf("Some text")).title == "esite 2026"


def test_pdf_without_text_is_rejected() -> None:
    with pytest.raises(UnsupportedFile, match="no extractable text"):
        parse_file("scan.pdf", make_pdf(None))


@pytest.mark.parametrize("name", ["bad.pdf", "bad.docx"])
def test_corrupt_files_raise_domain_error(name: str) -> None:
    with pytest.raises(UnsupportedFile):
        parse_file(name, b"this is not a real document")


def test_unsupported_extension() -> None:
    with pytest.raises(UnsupportedFile):
        parse_file("data.xlsx", b"abc")
    with pytest.raises(UnsupportedFile):
        parse_file("noext", b"abc")


def test_empty_text_file_rejected() -> None:
    with pytest.raises(UnsupportedFile):
        parse_file("empty.txt", b"  \n ")


def test_size_cap() -> None:
    with pytest.raises(FileTooLarge):
        parse_file("big.txt", b"x" * (MAX_FILE_BYTES + 1))
    assert parse_file("ok.txt", b"x" * 1000).text
