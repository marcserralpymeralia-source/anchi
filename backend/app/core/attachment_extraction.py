from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree


MAX_EXTRACTED_TEXT_CHARS = 200_000
SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/amr",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
}
SUPPORTED_AUDIO_EXTENSIONS = {".amr", ".m4a", ".mp3", ".ogg", ".wav", ".webm"}
SUPPORTED_DOCUMENT_MIME_TYPES = {
    "application/msword",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
SUPPORTED_DOCUMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".txt"}


@dataclass(frozen=True, slots=True)
class AttachmentExtraction:
    text: str | None
    status: str
    error: str | None = None


def extract_attachment_text(
    content: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> AttachmentExtraction:
    """Extract text from supported document attachments without calling an LLM."""
    normalized_type = str(content_type or "").strip().lower().split(";", 1)[0]
    extension = Path(str(filename or "")).suffix.lower()

    if normalized_type in SUPPORTED_AUDIO_MIME_TYPES or extension in SUPPORTED_AUDIO_EXTENSIONS:
        return AttachmentExtraction(
            text=None,
            status="transcription_pending",
            error="Audio guardado; requiere transcripcion antes de enviarse al pipeline.",
        )
    if normalized_type == "application/pdf" or extension == ".pdf":
        return _extract_pdf(content)
    if normalized_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or extension == ".docx":
        return _extract_docx(content)
    if normalized_type == "text/plain" or extension == ".txt":
        return _extract_text_file(content)
    if normalized_type in SUPPORTED_DOCUMENT_MIME_TYPES or extension in SUPPORTED_DOCUMENT_EXTENSIONS:
        return AttachmentExtraction(
            text=None,
            status="unsupported",
            error="El formato de documento no tiene extractor disponible.",
        )
    return AttachmentExtraction(text=None, status="unsupported", error="Tipo de adjunto no soportado.")


def _extract_pdf(content: bytes) -> AttachmentExtraction:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        normalized = _normalize_text(text)
        if normalized:
            return AttachmentExtraction(text=normalized, status="extracted")
        return AttachmentExtraction(
            text=None,
            status="no_text_found",
            error="El PDF no contiene texto legible.",
        )
    except Exception as exc:  # noqa: BLE001
        fallback = _extract_pdf_fallback(content)
        if fallback:
            return AttachmentExtraction(text=fallback, status="extracted")
        return AttachmentExtraction(
            text=None,
            status="extraction_error",
            error=f"No se pudo leer el PDF: {exc}",
        )


def _extract_pdf_fallback(content: bytes) -> str:
    raw = content.decode("latin-1", errors="ignore")
    chunks: list[str] = []
    for match in re.finditer(r"\((.*?)\)\s*Tj", raw, re.S):
        chunks.append(_decode_pdf_text(match.group(1)))
    for match in re.finditer(r"\[(.*?)\]\s*TJ", raw, re.S):
        chunks.extend(_decode_pdf_text(item) for item in re.findall(r"\((.*?)\)", match.group(1), re.S))
    return _normalize_text("\n".join(item for item in chunks if item.strip()))


def _decode_pdf_text(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    value = re.sub(r"\\([nrtbf])", " ", value)
    value = re.sub(r"\\[0-7]{1,3}", " ", value)
    return value.strip()


def _extract_docx(content: bytes) -> AttachmentExtraction:
    try:
        with ZipFile(BytesIO(content)) as archive:
            document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
    except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
        return AttachmentExtraction(text=None, status="extraction_error", error=f"No se pudo leer el DOCX: {exc}")

    paragraph_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    text_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    paragraphs: list[str] = []
    for paragraph in root.iter(paragraph_tag):
        value = "".join(node.text or "" for node in paragraph.iter(text_tag)).strip()
        if value:
            paragraphs.append(value)
    text = _normalize_text("\n".join(paragraphs))
    if text:
        return AttachmentExtraction(text=text, status="extracted")
    return AttachmentExtraction(text=None, status="no_text_found", error="El DOCX no contiene texto legible.")


def _extract_text_file(content: bytes) -> AttachmentExtraction:
    if not content:
        return AttachmentExtraction(text=None, status="no_text_found", error="El archivo de texto esta vacio.")
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = _normalize_text(content.decode(encoding))
        except UnicodeDecodeError:
            continue
        if text:
            return AttachmentExtraction(text=text, status="extracted")
        return AttachmentExtraction(text=None, status="no_text_found", error="El archivo de texto esta vacio.")
    return AttachmentExtraction(text=None, status="extraction_error", error="No se pudo decodificar el archivo de texto.")


def _normalize_text(value: str) -> str:
    normalized = "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()
    return normalized[:MAX_EXTRACTED_TEXT_CHARS]
