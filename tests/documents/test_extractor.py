import zipfile
from pathlib import Path

from uribo_mcp.documents import DocumentLimits, OcrBackend, extract_document, sha256_file


class StubOcr(OcrBackend):
    def image_to_text(self, image_path: Path, *, language: str = "jpn+eng") -> str:
        return "画像の文字"


def test_text_is_hashed_chunked_and_bounded(tmp_path):
    path = tmp_path / "lecture.txt"
    path.write_text("alpha beta gamma delta", encoding="utf-8")
    result = extract_document(path, limits=DocumentLimits(chunk_chars=10))
    assert result.status == "ok"
    assert result.sha256 == sha256_file(path)
    assert result.pages[0].text == "alpha beta gamma delta"
    assert result.chunks


def test_size_limit_and_unknown_extension_are_explicit(tmp_path):
    too_large = tmp_path / "large.txt"
    too_large.write_text("12345", encoding="utf-8")
    assert "size_limit_exceeded" in extract_document(too_large, limits=DocumentLimits(max_bytes=2)).error
    unknown = tmp_path / "archive.bin"
    unknown.write_bytes(b"x")
    assert extract_document(unknown).status == "unsupported"


def test_images_require_or_use_an_injected_ocr_backend(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"not a real image; stub does not inspect it")
    assert extract_document(image).status == "unsupported"
    result = extract_document(image, ocr_backend=StubOcr())
    assert result.status == "ok"
    assert result.pages[0].source == "ocr"
    assert result.pages[0].text == "画像の文字"


def test_office_open_xml_fallbacks_extract_document_slide_and_sheet_text(tmp_path):
    docx = tmp_path / "lecture.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<w:document xmlns:w='w'><w:body><w:p><w:t>文書本文</w:t></w:p></w:body></w:document>")
    pptx = tmp_path / "slides.pptx"
    with zipfile.ZipFile(pptx, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", "<p:sld xmlns:p='p'><p:t>スライド本文</p:t></p:sld>")
    xlsx = tmp_path / "sheet.xlsx"
    with zipfile.ZipFile(xlsx, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst xmlns='x'><si><t>セル値</t></si></sst>")
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet xmlns='x'><sheetData><row><c t='s'><v>0</v></c></row></sheetData></worksheet>")
    assert "文書本文" in extract_document(docx).pages[0].text
    assert "スライド本文" in extract_document(pptx).pages[0].text
    assert "セル値" in extract_document(xlsx).pages[0].text
