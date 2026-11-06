"""Text extraction, chunking and topic relevance for study material."""

from __future__ import annotations

import io
import re
from collections import Counter

from purdue_brightspace_mcp.sanitize import html_to_text, redact_identifiers

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".htm")
MAX_PDF_PAGES = 300
MAX_TEXT_CHARS = 400_000

# Academic integrity: never turn graded assessments or their answers into study
# cards automatically. Instructor-provided practice material can still be added
# by hand if the instructor allows it.
ASSESSMENT_TITLE = re.compile(
    r"\b(quiz(zes)?|exams?|midterms?|final exam|answer key|solutions?|graded)\b", re.IGNORECASE
)

_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
}


class UnsupportedFile(ValueError):
    pass


def extension_for(filename: str | None, content_type: str | None) -> str:
    name = (filename or "").lower()
    for ext in SUPPORTED_EXTENSIONS:
        if name.endswith(ext):
            return ext
    base = (content_type or "").split(";")[0].strip().lower()
    if base in _TYPES:
        return _TYPES[base]
    raise UnsupportedFile(
        f"{filename or 'This file'} is not a supported type. Use PDF, Word, PowerPoint, text or HTML."
    )


def extract_text(data: bytes, filename: str | None = None, content_type: str | None = None) -> str:
    ext = extension_for(filename, content_type)
    try:
        if ext == ".pdf":
            from pypdf import PdfReader  # noqa: PLC0415

            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
        elif ext == ".docx":
            import docx  # noqa: PLC0415

            document = docx.Document(io.BytesIO(data))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
        elif ext == ".pptx":
            from pptx import Presentation  # noqa: PLC0415

            deck = Presentation(io.BytesIO(data))
            slides = []
            for slide in deck.slides:
                lines = [s.text_frame.text for s in slide.shapes if getattr(s, "has_text_frame", False)]
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                    lines.append(slide.notes_slide.notes_text_frame.text)
                slides.append("\n".join(x for x in lines if x.strip()))
            text = "\n\n".join(slides)
        elif ext in (".html", ".htm"):
            text = html_to_text(data.decode("utf-8", errors="replace"))
        else:
            text = data.decode("utf-8", errors="replace")
    except UnsupportedFile:
        raise
    except Exception as exc:  # malformed documents
        raise UnsupportedFile(f"Could not read {filename or 'the file'}: it may be damaged or protected.") from exc
    return clean_text(text)


def clean_text(text: str) -> str:
    """Normalize whitespace and remove email addresses and PUID-like numbers."""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return redact_identifiers(text.strip())[:MAX_TEXT_CHARS]


def chunk_text(text: str, size: int = 1200) -> list[str]:
    """Split text into roughly `size`-character chunks on paragraph boundaries."""
    chunks: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            cut = para.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 3 else size
            chunks.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) >= 40]


STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in into is it its of on or that the
    their this to was were what when where which who why will with about over under than then
    these those such can could should would may might also more most other some any each""".split()
)


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-']{1,}", text.lower()) if w not in STOPWORDS]


def relevance(chunk: str, topic: str) -> float:
    """How strongly a chunk is about `topic`: share of topic terms present, weighted by frequency."""
    wanted = set(terms(topic))
    if not wanted:
        return 0.0
    counts = Counter(terms(chunk))
    present = [t for t in wanted if counts[t] or any(k.startswith(t) for k in counts)]
    if not present:
        return 0.0
    frequency = sum(counts[t] for t in present)
    phrase_bonus = 1.0 if topic.lower().strip() in chunk.lower() else 0.0
    return len(present) / len(wanted) + min(frequency, 10) / 20 + phrase_bonus
