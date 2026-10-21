"""Heading-aware chunking.

Why not fixed-width character windows
-------------------------------------
SOP text is organised by `##` sections, and a section is the smallest unit that means
anything on its own ("免用期" without its heading is ambiguous). Splitting mid-section
produces chunks that retrieve well but read badly, and the citation then points at a
heading that does not contain the claim. So: never merge across headings, and always keep
the heading attached to the chunk text used for embedding.

Overlap exists because a rule and its exception often straddle a paragraph boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 520
OVERLAP_CHARS = 90

_HEADING = re.compile(r'^(#{1,3})\s+(.+)$', re.M)
_SENTENCE_END = re.compile(r'(?<=[。！？；.!?])\s*')


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str | None
    text: str

    def embed_text(self) -> str:
        """Text used for the vector: the heading carries a lot of the signal."""
        return f'{self.heading}\n{self.text}' if self.heading else self.text


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Split into (heading, body) pairs. Text before the first heading keeps `None`."""
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [(None, text.strip())] if text.strip() else []

    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            sections.append((m.group(2).strip(), body))
    return sections


def _pack(paragraphs: list[str], max_chars: int, overlap_chars: int) -> list[str]:
    """Greedily pack paragraphs, carrying a sentence of overlap into the next window."""
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        if size + len(para) <= max_chars or not current:
            current.append(para)
            size += len(para)
            continue
        windows.append('\n'.join(current))
        tail = _tail(current, overlap_chars)
        current = [tail, para] if tail else [para]
        size = sum(len(x) for x in current)
    if current:
        windows.append('\n'.join(current))
    return [w.strip() for w in windows if w.strip()]


def _tail(parts: list[str], overlap_chars: int) -> str:
    text = ' '.join(parts).strip()
    if len(text) <= overlap_chars:
        return text
    sentences = [s for s in _SENTENCE_END.split(text) if s.strip()]
    tail = ''
    for sentence in reversed(sentences):
        if len(tail) + len(sentence) > overlap_chars and tail:
            break
        tail = sentence + tail
    return tail.strip() or text[-overlap_chars:]


def chunk_markdown(text: str, *, max_chars: int = MAX_CHARS,
                   overlap_chars: int = OVERLAP_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    for heading, body in _split_sections(text):
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', body) if p.strip()]
        for window in _pack(paragraphs, max_chars, overlap_chars):
            chunks.append(Chunk(ordinal=ordinal, heading=heading, text=window))
            ordinal += 1
    return chunks
