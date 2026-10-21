"""SOP knowledge base: chunking, embedding, hybrid retrieval, cited answering.

This package exists so the agent can answer "how do we handle X" from the company's own
operating procedures **with citations**, and refuse when the corpus does not cover the
question. Both properties are requirements, not niceties: an SOP answer that cannot be
traced to a document is unusable in an audit, and an answer invented for an uncovered
question is worse than no answer.

Design boundary
---------------
Retrieval never mutates business state. It reads documents and returns text plus
provenance. Nothing here can price, approve, or send anything.
"""
from app.knowledge.service import (
    AnswerResult,
    IngestResult,
    SearchHit,
    answer_question,
    ingest_document,
    seed_corpus,
    search_knowledge,
)

__all__ = [
    'AnswerResult',
    'IngestResult',
    'SearchHit',
    'answer_question',
    'ingest_document',
    'seed_corpus',
    'search_knowledge',
]
