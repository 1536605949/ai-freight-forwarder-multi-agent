"""Hybrid retrieval: BM25 + vector + rerank, with an absolute evidence score.

Why not plain reciprocal rank fusion
------------------------------------
The first implementation fused BM25 and vector ranks with RRF and thresholded the result.
Measured on the bundled corpus it gave "公司食堂几点开饭" a top score of 0.67 -- because RRF
only knows *rank*, and with any corpus there is always something ranked first. Rank fusion
is the right tool for ordering candidates and the wrong tool for deciding whether the corpus
covers the question at all.

So ranking and sufficiency are separated:

* **Ranking** uses BM25, cosine and reciprocal rank fusion.
* **Sufficiency** is an absolute score built from idf-weighted query-term coverage, the
  magnitude of the lexical match, and cosine. It does not depend on what else is in the
  corpus, which is what makes a fixed threshold meaningful.

A chunk that shares no content term with the query scores exactly 0. That is a fact, not a
tuning choice: text that contains none of the question's terms cannot answer it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.knowledge.embeddings import cjk_char_count, covered_char_count, tokenize

RRF_K = 60
_W_COVERAGE = 0.42
_W_LEXICAL = 0.26
_W_VECTOR = 0.16
_W_HEADING = 0.10
_W_RRF = 0.06

# Query-side only. CJK bigrams make "什么" / "怎么" into content terms, and on a small
# corpus their idf is *high* simply because they appear in few chunks -- so a question about
# Python scored 0.54 against a shipping SOP purely on function words. Document-side
# tokenisation is left alone: a stopword in the text is harmless, a stopword counted as
# evidence is not.
QUERY_STOPWORDS = frozenset({
    '什么', '怎么', '如何', '为什', '么办', '可以', '需要', '是否', '如果', '我们', '你们',
    '他们', '这个', '那个', '就是', '不是', '没有', '多少', '几点', '时候', '以及', '并且',
    '但是', '因为', '所以', '问题', '一个', '这样', '那样', '哪些', '哪个', '还能', '请问',
    '告诉', '一下', '一般', '具体', '相关', '情况', '进行', '已经', '还是', '或者',
    'the', 'is', 'are', 'was', 'were', 'of', 'to', 'and', 'or', 'in', 'on',
    'for', 'with', 'what', 'how', 'why', 'do', 'does', 'did', 'you', 'it', 'this',
    'that', 'be', 'can', 'should', 'would',
})

# Below this total idf mass the query carries no domain content.
MIN_QUERY_WEIGHT = 0.8

# Fraction of the question's characters that the corpus vocabulary recognises at all.
# This is the out-of-domain gate, and it is deliberately query-level: it answers "does this
# knowledge base speak about this subject", not "which chunk is closest". A question made of
# words the corpus has never seen must be refused even if one chunk happens to share a term.
MIN_DOMAIN_RATIO = 0.35


def content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in QUERY_STOPWORDS}


def _query_char_mass(query: str, tokens: set[str]) -> int:
    """Characters the question is made of: all CJK chars plus latin word characters."""
    latin = sum(len(t) for t in tokens if t.isascii())
    return cjk_char_count(query) + latin


@dataclass(frozen=True)
class IndexedChunk:
    id: str
    document_id: str
    document_title: str
    heading: str | None
    ordinal: int
    text: str
    embedding: list[float]
    embedding_model: str


@dataclass(frozen=True)
class Scored:
    chunk: IndexedChunk
    score: float
    lexical: float
    vector: float
    coverage: float
    heading_hit: float

    def as_dict(self) -> dict:
        return {
            'chunk_id': self.chunk.id,
            'document_id': self.chunk.document_id,
            'document_title': self.chunk.document_title,
            'heading': self.chunk.heading,
            'ordinal': self.chunk.ordinal,
            'text': self.chunk.text,
            'score': round(self.score, 4),
            'signals': {
                'bm25': round(self.lexical, 4),
                'cosine': round(self.vector, 4),
                'coverage': round(self.coverage, 4),
                'heading_hit': round(self.heading_hit, 2),
            },
        }


class _Bm25:
    def __init__(self, chunks: list[IndexedChunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tokens = [tokenize(c.heading + '\n' + c.text if c.heading else c.text) for c in chunks]
        self.lengths = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for tokens in self.tokens:
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.tf.append(counts)
            for token in counts:
                df[token] = df.get(token, 0) + 1
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query_tokens: list[str]) -> list[float]:
        out: list[float] = []
        for counts, length in zip(self.tf, self.lengths):
            total = 0.0
            for token in query_tokens:
                tf = counts.get(token)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1.0))
                total += self.idf.get(token, 0.0) * tf * (self.k1 + 1) / denom
            out.append(total)
        return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    ranks = [0] * len(scores)
    for position, index in enumerate(order):
        ranks[index] = position + 1
    return ranks


def rank(query: str, chunks: list[IndexedChunk], query_embedding: list[float], *,
         k: int = 5) -> list[Scored]:
    if not chunks:
        return []

    query_tokens = tokenize(query)
    unique_query = content_terms(query)
    if not unique_query:
        return []

    bm25 = _Bm25(chunks)
    lexical_scores = bm25.scores(query_tokens)
    vector_scores = [_cosine(query_embedding, c.embedding) for c in chunks]
    lexical_ranks = _ranks(lexical_scores)
    vector_ranks = _ranks(vector_scores)

    # Out-of-domain gate, computed once for the query against the whole corpus vocabulary.
    # It answers "does this knowledge base speak about this subject", independently of which
    # chunk happens to rank first. "怎么申请办公用品" is refused here, not by the threshold.
    #
    # Function-word n-grams are excluded from the vocabulary used for coverage. Without that,
    # "离职流程是什么" passed the gate on the strength of "什么" alone (which the corpus does
    # contain, in "需要什么资料") and scored 0.68 against a demurrage SOP.
    vocabulary = {t for c in chunks for t in tokenize((c.heading or '') + '\n' + c.text)}
    coverage_vocabulary = {t for t in vocabulary if t not in QUERY_STOPWORDS}
    char_mass = cjk_char_count(query) + sum(len(t) for t in unique_query if t.isascii())
    if char_mass:
        known = covered_char_count(query, coverage_vocabulary)
        latin_known = sum(len(t) for t in unique_query
                          if t.isascii() and t in coverage_vocabulary)
        if (known + latin_known) / char_mass < MIN_DOMAIN_RATIO:
            return []

    # Only n-grams the corpus actually contains take part in ranking. Absent n-grams are
    # handled by the gate above; letting them into the idf denominator would punish a good
    # question for the junk n-grams that overlapping windows produce ("柜了", "了怎"),
    # which is what kept "甩柜了怎么处理" down at 0.31.
    known_query = {t for t in unique_query if t in vocabulary}
    if not known_query:
        return []

    weights = {t: max(0.0, bm25.idf.get(t, 0.0)) for t in known_query}
    total_weight = sum(weights.values())
    if total_weight < MIN_QUERY_WEIGHT:
        return []

    scored: list[Scored] = []
    for i, chunk in enumerate(chunks):
        chunk_tokens = set(tokenize(chunk.heading + '\n' + chunk.text
                                    if chunk.heading else chunk.text))
        matched = known_query & chunk_tokens
        if not matched:
            # No shared content term: this chunk cannot answer the question.
            scored.append(Scored(chunk=chunk, score=0.0, lexical=lexical_scores[i],
                                 vector=vector_scores[i], coverage=0.0, heading_hit=0.0))
            continue

        weighted_coverage = sum(weights[t] for t in matched) / total_weight
        # Saturating transform: BM25 is unbounded, and a 40-chunk corpus produces values
        # that differ by an order of magnitude for reasons unrelated to relevance.
        lexical_norm = 1.0 - math.exp(-max(0.0, lexical_scores[i]) / 4.0)
        vector_norm = max(0.0, vector_scores[i])
        heading_hit = 1.0 if chunk.heading and (matched & set(tokenize(chunk.heading))) else 0.0
        rrf = 1.0 / (RRF_K + lexical_ranks[i]) + 1.0 / (RRF_K + vector_ranks[i])
        rrf_norm = min(1.0, rrf / (2.0 / (RRF_K + 1)))

        score = (_W_COVERAGE * weighted_coverage + _W_LEXICAL * lexical_norm
                 + _W_VECTOR * vector_norm + _W_HEADING * heading_hit + _W_RRF * rrf_norm)
        scored.append(Scored(chunk=chunk, score=score, lexical=lexical_scores[i],
                             vector=vector_scores[i], coverage=weighted_coverage,
                             heading_hit=heading_hit))

    scored.sort(key=lambda s: (-s.score, s.chunk.document_id, s.chunk.ordinal))
    return scored[:k]
