"""Embedding providers.

The default is a deterministic hashing embedder, not a neural model. That is a deliberate
choice and it is stated plainly so nobody mistakes it for semantic search:

* It needs no key, no network, and no GPU, so `AGENT_PROVIDER=mock` still runs the whole
  RAG path offline and the eval is reproducible on any machine.
* Cosine similarity over these vectors behaves like a *smoothed lexical match*. It will
  not bridge "滞箱费" and "demurrage" the way a trained model does. The BM25 channel and
  the reranker carry the rest of the quality, and the eval reports retrieval quality
  honestly rather than claiming semantic understanding it does not have.
* Swapping in a real model is a config change (`EMBEDDING_PROVIDER=openai`), not a rewrite,
  because both implement the same two-method interface.

The vector is stored alongside the `embedding_model` name, so a corpus embedded by one
model can never be silently queried by another — mismatched spaces produce plausible
garbage, which is the worst failure mode for retrieval.

Tokenisation
------------
Chinese has no spaces. Two approaches were tried and rejected before this one:

* character bigrams: produced pieces straddling word boundaries ("柜了", "了怎") which then
  counted as evidence -- "Python 的 GIL 是什么" scored 0.54 against a shipping SOP.
* a curated domain lexicon with maximal matching: brittle. Any term missing from the list
  disappears from the query, so "甩柜了怎么处理" tokenised to just "甩柜" because "处理" was
  not enumerated.

Overlapping character n-grams (2..4) have neither problem: every shared substring is found,
nothing depends on a hand-maintained list, and the noise is handled downstream by idf
weighting plus the character-coverage gate in `retrieval.py`. For a fixed corpus this is
both simpler and more robust than a dictionary.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.config import get_settings

DIM = 256
_CJK = re.compile(r'[\u4e00-\u9fff]+')
_LATIN = re.compile(r'[a-z][a-z0-9]*|\d+(?:\.\d+)?')
_NGRAM_SIZES = (2, 3, 4)


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def _ngrams(run: str) -> list[str]:
    return [run[i:i + n] for n in _NGRAM_SIZES for i in range(len(run) - n + 1)]


def tokenize(text: str) -> list[str]:
    """Latin words (2+ chars) plus overlapping 2..4 character Chinese n-grams."""
    low = text.lower()
    tokens = [t for t in _LATIN.findall(low) if len(t) > 1]
    for run in _CJK.findall(low):
        tokens.extend(_ngrams(run))
    return tokens


def cjk_runs(text: str) -> list[str]:
    return _CJK.findall(text)


def cjk_char_count(text: str) -> int:
    return sum(len(run) for run in _CJK.findall(text))


def covered_char_count(text: str, vocabulary: set[str]) -> int:
    """Query characters belonging to at least one n-gram the vocabulary knows.

    Counting *characters* rather than n-grams is what keeps the out-of-domain gate fair:
    "甩柜了怎么处理" yields n-grams like "柜了" that no corpus contains, and counting those
    as misses would reject a perfectly good question. A character is covered if any of the
    n-grams spanning it is known, so the junk n-grams cost nothing.
    """
    covered = 0
    for run in _CJK.findall(text):
        hit: set[int] = set()
        for n in _NGRAM_SIZES:
            for i in range(len(run) - n + 1):
                if run[i:i + n] in vocabulary:
                    hit.update(range(i, i + n))
        covered += len(hit)
    return covered


class HashingEmbedder:
    name = 'hashing-v1'
    dim = DIM

    def embed(self, text: str) -> list[float]:
        counts: dict[int, float] = {}
        signs: dict[int, float] = {}
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode('utf-8'), digest_size=8).digest()
            value = int.from_bytes(digest, 'big')
            bucket = value % DIM
            sign = 1.0 if (value >> 63) & 1 else -1.0
            counts[bucket] = counts.get(bucket, 0.0) + 1.0
            signs[bucket] = sign

        vector = [0.0] * DIM
        for bucket, count in counts.items():
            # Sublinear term frequency: the tenth occurrence of a term says little more
            # than the second, and linear scaling lets one repeated word dominate.
            vector[bucket] = signs[bucket] * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class OpenAIEmbedder:
    """Real embeddings via an OpenAI-compatible endpoint. Only used when configured."""

    def __init__(self, *, model: str, base_url: str, api_key: str) -> None:
        self.name = f'openai:{model}'
        self.dim = 1536
        self._model = model
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        import httpx

        response = httpx.post(
            f'{self._base_url}/embeddings',
            headers={'Authorization': f'Bearer {self._api_key}'},
            json={'model': self._model, 'input': texts},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        rows = sorted(payload['data'], key=lambda d: d['index'])
        vectors = [row['embedding'] for row in rows]
        if vectors:
            self.dim = len(vectors[0])
        return vectors


def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == 'openai':
        if not (s.embedding_base_url and s.embedding_api_key):
            raise RuntimeError(
                'EMBEDDING_PROVIDER=openai requires EMBEDDING_BASE_URL and EMBEDDING_API_KEY; '
                'refusing to fall back silently, because a corpus embedded by one model and '
                'queried by another returns plausible nonsense.'
            )
        return OpenAIEmbedder(model=s.embedding_model, base_url=s.embedding_base_url,
                              api_key=s.embedding_api_key)
    return HashingEmbedder()
