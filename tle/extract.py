"""Turns an uploaded file into text the tagger and the database can use."""

from __future__ import annotations

import io
import mimetypes
import re

from .config import Config
from .llm import LLM, Image

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}
CAPTION_EXTENSIONS = {".vtt", ".srt"}

PAGES_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "All the text on these pages, word for word, in reading order. Charts, diagrams "
                           "and photos are described briefly in [square brackets] where they appear.",
        },
    },
    "required": ["text"],
}

IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "visible_text": {"type": "string", "description": "All legible text in the image, verbatim."},
        "description": {"type": "string", "description": "What the image shows and the point it makes."},
    },
    "required": ["visible_text", "description"],
}


def guess_mime(filename: str, mime_type: str | None = None) -> str:
    if mime_type and mime_type != "application/octet-stream":
        return mime_type
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _ext(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _captions_to_text(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    return "\n".join(lines)


def extract_text(data: bytes, filename: str, config: Config, mime_type: str | None = None) -> str:
    mime = guess_mime(filename, mime_type)
    ext = _ext(filename)

    if ext in CAPTION_EXTENSIONS:
        return _captions_to_text(data.decode("utf-8", errors="replace"))
    if ext in TEXT_EXTENSIONS or mime.startswith("text/"):
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf" or mime == "application/pdf":
        return _pdf(data, filename, config)
    if ext == ".docx":
        import docx

        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)
    if mime.startswith("image/"):
        result = LLM(config.reader).generate_json(
            system="You describe images so they can be searched and quoted later.",
            prompt="Transcribe any text in this image, then describe what it shows and the point it makes.",
            schema=IMAGE_SCHEMA,
            images=[Image(data=data, mime_type=mime)],
        )
        return f"{result['description']}\n\nText in image:\n{result['visible_text']}".strip()
    if mime.startswith("audio/") or mime.startswith("video/"):
        if not config.transcription:
            raise ValueError("Audio and video need an OpenAI or Gemini model, picked under Models in Settings.")
        return LLM(config.transcription).transcribe(data, mime, filename)

    raise ValueError(f"Can't read {filename} ({mime}). Send text, PDF, DOCX, VTT/SRT, an image or audio.")


def _pdf(data: bytes, filename: str, config: Config) -> str:
    """Reads the PDF's text layer. Scanned PDFs and slides saved as pictures have little or
    none, so when there's less than `min_chars_per_page` on average the pages are rendered
    as images and read by the model instead (`sources.files.pdf_fallback`)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    pages = max(len(reader.pages), 1)
    settings = config.files()
    if len(text) / pages >= int(settings.get("min_chars_per_page", 100)):
        return text
    if settings.get("pdf_fallback", "llm") != "llm":
        if text:
            return text
        raise ValueError(f"{filename} has no text layer (it's probably scanned). "
                         "Set sources.files.pdf_fallback to llm to have the model read it.")
    read = _pdf_with_llm(data, config)
    return read if len(read) > len(text) else text


def _pdf_with_llm(data: bytes, config: Config) -> str:
    import pypdfium2 as pdfium

    settings = config.files()
    per_call = int(settings.get("pages_per_call", 8))
    max_pages = int(settings.get("max_pdf_pages", 50))
    pdf = pdfium.PdfDocument(data)
    total = len(pdf)
    llm = LLM(config.reader)
    parts = []
    for start in range(0, min(total, max_pages), per_call):
        images = []
        for index in range(start, min(start + per_call, total, max_pages)):
            page = pdf[index]
            width, height = page.get_size()
            # About 1,600 pixels on the long edge: readable, and close to what models resize to anyway.
            bitmap = page.render(scale=1600 / max(width, height))
            buffer = io.BytesIO()
            bitmap.to_pil().convert("RGB").save(buffer, format="JPEG", quality=80)
            images.append(Image(data=buffer.getvalue(), mime_type="image/jpeg"))
        result = llm.generate_json(
            system="You transcribe document pages so they can be searched and quoted later.",
            prompt=f"These are pages {start + 1} to {start + len(images)} of a PDF. Transcribe them.",
            schema=PAGES_SCHEMA,
            images=images,
            max_tokens=16000,
        )
        parts.append(result.get("text", "").strip())
    pdf.close()
    text = "\n\n".join(p for p in parts if p)
    if total > max_pages:
        text += f"\n\n[Only the first {max_pages} of {total} pages were read.]"
    if not text.strip():
        raise ValueError("Couldn't find any text in this PDF, even reading the pages as images.")
    return text
