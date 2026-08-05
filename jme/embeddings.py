"""Embedding providers.

Two implementations behind one interface:

  * `hash` - deterministic, offline, zero cost. Random-projection bag of character
    n-grams. Not semantically meaningful, but it makes the entire pipeline runnable
    and testable without an API key, and retrieval tests can assert exact behaviour.
  * `voyage` - real embeddings for real use.

The dimension is fixed at models.EMBEDDING_DIM because the pgvector column is fixed.
A provider whose native dimension differs is projected or zero-padded to fit, and that
is recorded in `evidence_chunk.embedding_model` so mixed-provider corpora are visible.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

from jme.config import get_settings
from jme.models import EMBEDDING_DIM


class EmbeddingProvider(ABC):
    name: str
    dim: int = EMBEDDING_DIM

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    words = _TOKEN_RE.findall(lowered)
    # strict=False is the point: the second sequence is deliberately one shorter, so
    # the trailing word has no bigram partner.
    grams = ["".join(pair) for pair in zip(words, words[1:], strict=False)]
    return words + grams


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic hashed bag-of-tokens with L2 normalization.

    Cosine similarity over these vectors is a decent lexical-overlap proxy: shared
    tokens push similarity up, and it never needs the network. Good enough to develop
    and test the ranking pipeline against; swap to voyage before trusting the numbers.
    """

    name = "hash-v1"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embeddings. Anthropic does not serve an embeddings endpoint."""

    name = "voyage"

    def __init__(self, model: str, api_key: str, dim: int = EMBEDDING_DIM) -> None:
        self.model = model
        self.api_key = api_key
        self.dim = dim
        self.name = f"voyage:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - network
        import httpx

        response = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts, "input_type": "document"},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()["data"]
        return [self._fit(item["embedding"]) for item in sorted(data, key=lambda d: d["index"])]

    def _fit(self, vec: list[float]) -> list[float]:
        if len(vec) == self.dim:
            return vec
        if len(vec) < self.dim:
            return vec + [0.0] * (self.dim - len(vec))
        return vec[: self.dim]


def get_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_provider == "voyage":
        if not settings.voyage_api_key:
            raise RuntimeError("JME_EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is unset")
        return VoyageEmbeddingProvider(settings.embedding_model, settings.voyage_api_key)
    return HashEmbeddingProvider()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
