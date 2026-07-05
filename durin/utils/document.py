"""Document text extraction utilities for durin."""

import mimetypes
from pathlib import Path

from loguru import logger

from durin.utils.helpers import detect_image_mime

# Supported file extensions for text extraction
SUPPORTED_EXTENSIONS: set[str] = {
    # Document formats — converted to markdown via the shared markitdown
    # converter (durin/memory/doc_convert)
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".epub",
    ".ipynb",
    # Text formats
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    # Image formats (for future OCR support)
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}

_MAX_TEXT_LENGTH = 200_000


def extract_text(path: Path) -> str | None:
    """Extract text from a file.

    Args:
        path: Path to the file.

    Returns:
        Extracted text as string, None for unsupported types,
        or error string for failures.
    """
    if not isinstance(path, Path):
        path = Path(path)

    if not path.exists():
        return f"[error: file not found: {path}]"

    ext = path.suffix.lower()

    # Plain-text formats are read verbatim. Everything else markitdown can
    # parse (PDF, Office, EPUB, notebooks, …) goes through the single shared
    # converter (``durin/memory/doc_convert``) — one converter across durin,
    # producing clean markdown (headings, tables); its PPTX path covers the
    # grouped-shape / table cases the old bespoke per-format extractors did.
    if _is_text_extension(ext):
        return _extract_text_file(path)

    from durin.memory.doc_convert import is_convertible

    if is_convertible(ext):
        return _extract_via_markitdown(path)
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        # Image files - for future OCR support
        return f"[image: {path.name}]"
    # Unsupported extension
    return None


def _extract_via_markitdown(path: Path) -> str:
    """Convert a document to markdown via the shared doc_convert helper.

    The single converter for every non-plain-text format (PDF, Office, EPUB,
    notebooks, …). Returns a bracketed ``[error: …]`` string on an unsupported
    format or a conversion failure, matching the text-extraction contract.
    """
    from durin.memory.doc_convert import (
        DocConvertError,
        convert_file_to_markdown,
    )

    try:
        markdown = convert_file_to_markdown(path).markdown
    except DocConvertError as e:
        return f"[error: {e}]"
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to extract {}", path)
        return f"[error: failed to extract {path.suffix}: {e!s}]"
    return _truncate(markdown, _MAX_TEXT_LENGTH)


def _extract_text_file(path: Path) -> str:
    """Extract text from a plain text file."""
    try:
        # Try UTF-8 first, then latin-1 fallback
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="latin-1")
        return _truncate(content, _MAX_TEXT_LENGTH)
    except Exception as e:
        logger.exception("Failed to read text file {}", path)
        return f"[error: failed to read file: {e!s}]"


def _truncate(text: str, max_length: int) -> str:
    """Truncate text with a suffix indicating truncation."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... (truncated, {len(text)} chars total)"


def _is_text_extension(ext: str) -> bool:
    """Check if extension is a text format."""
    return ext in {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".html",
        ".htm",
        ".log",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
    }


# ---------------------------------------------------------------------------
# High-level helper: split media into images + extracted document text
# ---------------------------------------------------------------------------

_MAX_EXTRACT_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def extract_documents(
    text: str,
    media_paths: list[str],
    *,
    max_file_size: int = _MAX_EXTRACT_FILE_SIZE,
) -> tuple[str, list[str]]:
    """Separate images from documents in *media_paths*.

    Documents (PDF, DOCX, XLSX, PPTX, plain-text, …) have their text
    extracted and appended to *text*.  Only image paths are kept in the
    returned list so that downstream layers only need to handle vision
    blocks.

    Files larger than *max_file_size* bytes are skipped with a warning
    to avoid unbounded memory / CPU usage.
    """
    image_paths: list[str] = []
    doc_texts: list[str] = []

    for path_str in media_paths:
        p = Path(path_str)
        if not p.is_file():
            continue

        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > max_file_size:
            logger.warning(
                "Skipping oversized file for extraction: {} ({:.1f} MB > {} MB limit)",
                p.name, size / (1024 * 1024), max_file_size // (1024 * 1024),
            )
            continue

        with open(p, "rb") as f:
            header = f.read(16)
        mime = detect_image_mime(header) or mimetypes.guess_type(path_str)[0]
        if mime and mime.startswith("image/"):
            image_paths.append(path_str)
        else:
            extracted = extract_text(p)
            if extracted and not extracted.startswith("[error:"):
                doc_texts.append(f"[File: {p.name}]\n{extracted}")

    if doc_texts:
        text = text + "\n\n" + "\n\n".join(doc_texts)

    return text, image_paths
