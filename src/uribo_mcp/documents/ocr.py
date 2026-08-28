"""OCR backends. The command implementation is intentionally opt-in."""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from uribo_mcp.subprocesses import hidden_process_kwargs


class OcrBackend(ABC):
    @abstractmethod
    def image_to_text(self, image_path: Path, *, language: str = "jpn+eng") -> str:
        """Return recognised text or raise RuntimeError with a useful message."""


class TesseractCliBackend(OcrBackend):
    """OCR through a locally installed `tesseract` executable."""

    def __init__(
        self,
        executable: str = "tesseract",
        timeout_seconds: int = 120,
        default_language: str = "jpn+eng",
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.default_language = default_language

    def image_to_text(self, image_path: Path, *, language: str | None = None) -> str:
        selected_language = language or self.default_language
        try:
            completed = subprocess.run(
                [self.executable, str(image_path), "stdout", "-l", selected_language],
                capture_output=True, text=True, encoding="utf-8", timeout=self.timeout_seconds, check=False,
                **hidden_process_kwargs(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Tesseract is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Tesseract timed out") from exc
        if completed.returncode:
            message = completed.stderr.strip() or f"tesseract exited {completed.returncode}"
            raise RuntimeError(message)
        return completed.stdout.strip()
