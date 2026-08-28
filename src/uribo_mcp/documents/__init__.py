"""Local, bounded document text extraction."""

from .extractor import DocumentLimits, extract_document, sha256_file
from .models import DocumentChunk, DocumentPage, DocumentResult
from .ocr import OcrBackend, TesseractCliBackend

__all__ = ["DocumentLimits", "DocumentChunk", "DocumentPage", "DocumentResult", "OcrBackend", "TesseractCliBackend", "extract_document", "sha256_file"]
