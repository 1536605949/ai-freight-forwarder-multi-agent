"""Knowledge base service: ingest, search, and cited answering.

The answering strategy is extractive on purpose. A generative answer over retrieved SOP
chunks is exactly the place where a fluent, wrong sentence causes real operational damage
(a missed cut-off, a mis-declared dangerous good). Extractive answers can be checked
sentence-by-sentence against the citation, and the refusal path is meaningful: "no chunk
scored above the threshold" is a fact, whereas a model saying "I'm not sure" is a mood.

An LLM may still be used to *phrase* the result elsewhere. What it must not do is supply
the facts here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.knowledge.chunking import chunk_markdown
from app.knowledge.embeddings import Embedder, get_embedder, tokenize
from app.knowledge.retrieval import IndexedChunk, Scored, rank
from app.models import KnowledgeChunk, KnowledgeDocument
from app.services.common import audit, uid

CORPUS_DIR = Path(__file__).resolve().parent / 'corpus'
_SENTENCE = re.compile(r'(?<=[。！？；.!?])\s*')


@dataclass
class IngestResult:
    document_id: str
    title: str
    chunk_count: int
    created: bool
    embedding_model: str

    def as_dict(self) -> dict:
        return {'document_id': self.document_id, 'title': self.title,
                'chunk_count': self.chunk_count, 'created': self.created,
                'embedding_model': self.embedding_model}


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    document_title: str
    heading: str | None
    text: str
    score: float
    signals: dict

    def as_dict(self) -> dict:
        return {'chunk_id': self.chunk_id, 'document_id': self.document_id,
                'document_title': self.document_title, 'heading': self.heading,
                'text': self.text, 'score': self.score, 'signals': self.signals}


@dataclass
class AnswerResult:
    query: str
    answered: bool
    answer: str
    citations: list[dict] = field(default_factory=list)
    reason: str | None = None
    top_score: float = 0.0
    embedding_model: str = ''

    def as_dict(self) -> dict:
        return {'query': self.query, 'answered': self.answered, 'answer': self.answer,
                'citations': self.citations, 'reason': self.reason,
                'top_score': self.top_score, 'embedding_model': self.embedding_model}


def _checksum(title: str, text: str) -> str:
    normalised = re.sub(r'\r\n?', '\n', f'{title}\n{text}').strip()
    return hashlib.sha256(normalised.encode('utf-8')).hexdigest()


def ingest_document(db: Session, tenant_id: str, *, title: str, text: str,
                    source: str = 'manual', version: str = '1',
                    embedder: Embedder | None = None) -> IngestResult:
    embedder = embedder or get_embedder()
    checksum = _checksum(title, text)

    existing = (db.query(KnowledgeDocument)
                .filter_by(tenant_id=tenant_id, checksum=checksum).first())
    if existing:
        return IngestResult(existing.id, existing.title, existing.chunk_count, False,
                            embedder.name)

    chunks = chunk_markdown(text)
    if not chunks:
        raise ValueError('empty_document')

    document = KnowledgeDocument(id=uid('doc_'), tenant_id=tenant_id, title=title,
                                 source=source, version=version, checksum=checksum,
                                 content=text, chunk_count=len(chunks))
    db.add(document)
    vectors = embedder.embed_many([c.embed_text() for c in chunks])
    for chunk, vector in zip(chunks, vectors):
        db.add(KnowledgeChunk(
            id=uid('chk_'), tenant_id=tenant_id, document_id=document.id,
            ordinal=chunk.ordinal, heading=chunk.heading, text=chunk.text,
            embedding=vector, embedding_model=embedder.name,
        ))
    audit(db, tenant_id, 'system', 'knowledge.ingested', 'knowledge_document', document.id,
          {'title': title, 'chunks': len(chunks), 'embedding_model': embedder.name,
           'source': source})
    db.commit()
    db.refresh(document)
    return IngestResult(document.id, document.title, len(chunks), True, embedder.name)


def _load_index(db: Session, tenant_id: str, embedder: Embedder) -> list[IndexedChunk]:
    """Load only chunks embedded by the *current* model.

    Mixing embedding spaces produces confident nonsense, so a corpus embedded by a previous
    model is skipped rather than compared. The operator re-ingests to migrate.
    """
    rows = (db.query(KnowledgeChunk, KnowledgeDocument.title)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .filter(KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.embedding_model == embedder.name)
            .order_by(KnowledgeChunk.document_id, KnowledgeChunk.ordinal)
            .all())
    return [
        IndexedChunk(id=chunk.id, document_id=chunk.document_id, document_title=title,
                     heading=chunk.heading, ordinal=chunk.ordinal, text=chunk.text,
                     embedding=list(chunk.embedding or []), embedding_model=chunk.embedding_model)
        for chunk, title in rows
    ]


def _scored_to_hits(scored: list[Scored]) -> list[SearchHit]:
    return [
        SearchHit(chunk_id=s.chunk.id, document_id=s.chunk.document_id,
                  document_title=s.chunk.document_title, heading=s.chunk.heading,
                  text=s.chunk.text, score=round(s.score, 4),
                  signals={'bm25': round(s.lexical, 4), 'cosine': round(s.vector, 4),
                            'coverage': round(s.coverage, 4),
                            'heading_hit': s.heading_hit})
        for s in scored
    ]


def search_knowledge(db: Session, tenant_id: str, query: str, *, k: int | None = None,
                     min_score: float | None = None,
                     embedder: Embedder | None = None) -> list[SearchHit]:
    s = get_settings()
    embedder = embedder or get_embedder()
    query = (query or '').strip()
    if not query:
        return []
    index = _load_index(db, tenant_id, embedder)
    if not index:
        return []
    scored = rank(query, index, embedder.embed(query), k=k or s.knowledge_top_k)
    floor = s.knowledge_min_score if min_score is None else min_score
    return _scored_to_hits([x for x in scored if x.score >= floor])


def _best_sentences(text: str, query_tokens: set[str], limit: int = 2) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    scored = []
    for position, sentence in enumerate(sentences):
        tokens = {t for t in tokenize(sentence) if len(t) > 1}
        if not tokens:
            continue
        overlap = len(tokens & query_tokens) / len(query_tokens) if query_tokens else 0.0
        # A small positional prior: the first sentence of a section usually states the rule,
        # later ones the exception. Ties would otherwise be broken arbitrarily.
        scored.append((overlap + 0.02 / (position + 1), position, sentence))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen = [s for _, _, s in scored[:limit]]
    return chosen or sentences[:1]


def answer_question(db: Session, tenant_id: str, query: str, *,
                    k: int | None = None,
                    embedder: Embedder | None = None) -> AnswerResult:
    s = get_settings()
    embedder = embedder or get_embedder()
    query = (query or '').strip()
    if not query:
        return AnswerResult(query=query, answered=False, answer='',
                            reason='empty_query', embedding_model=embedder.name)

    index = _load_index(db, tenant_id, embedder)
    if not index:
        return AnswerResult(query=query, answered=False, answer='',
                            reason='knowledge_base_empty', embedding_model=embedder.name)

    scored = rank(query, index, embedder.embed(query), k=k or 3)
    floor = s.knowledge_min_score
    top = scored[0].score if scored else 0.0
    if not scored or top < floor:
        return AnswerResult(
            query=query, answered=False, answer='',
            reason='insufficient_evidence', top_score=round(top, 4),
            embedding_model=embedder.name,
        )

    query_tokens = {t for t in tokenize(query) if len(t) > 1}
    used: list[Scored] = []
    citations: list[dict] = []
    lines: list[str] = []
    for item in scored:
        if item.score < floor:
            continue
        sentences = _best_sentences(item.chunk.text, query_tokens)
        if not sentences:
            continue
        lines.append(' '.join(sentences))
        used.append(item)
        citations.append({
            'document_id': item.chunk.document_id,
            'document_title': item.chunk.document_title,
            'heading': item.chunk.heading,
            'chunk_id': item.chunk.id,
            'score': round(item.score, 4),
        })

    if not used:
        return AnswerResult(query=query, answered=False, answer='',
                            reason='insufficient_evidence', top_score=round(top, 4),
                            embedding_model=embedder.name)

    return AnswerResult(query=query, answered=True, answer='\n'.join(lines),
                        citations=citations, top_score=round(top, 4),
                        embedding_model=embedder.name)


def seed_corpus(db: Session, tenant_id: str, *,
                embedder: Embedder | None = None,
                directory: Path | None = None) -> list[IngestResult]:
    """Load every markdown SOP in the bundled corpus. Idempotent by checksum."""
    directory = directory or CORPUS_DIR
    embedder = embedder or get_embedder()
    results: list[IngestResult] = []
    for path in sorted(directory.glob('*.md')):
        text = path.read_text(encoding='utf-8')
        title = _first_heading(text) or path.stem
        results.append(ingest_document(db, tenant_id, title=title, text=text,
                                       source=f'corpus/{path.name}', embedder=embedder))
    return results


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return None
