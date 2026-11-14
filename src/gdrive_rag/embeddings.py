"""OpenAI-compatible embedding client (default: OpenRouter + qwen/qwen3-embedding-8b).

Vectors are truncated client-side to ``dimensions`` and re-normalized (Matryoshka-style), so the
request never depends on a provider honouring a ``dimensions`` parameter. Only queries get the
Qwen3 instruction prefix ``Instruct: <task>\\nQuery:<query>``; documents are sent as-is.
Adapted from local-rag-mcp ``OpenAIEmbeddings`` (Apache-2.0).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings

BATCH_SIZE = 32
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class EmbeddingError(RuntimeError):
    """Remote embedding call failed; the message never contains the API key."""


class EmbeddingClient(Protocol):
    @property
    def identity(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def query_text(instruction: str, query: str) -> str:
    return f"Instruct: {instruction}\nQuery:{query}" if instruction else query


def truncate_normalize(vector: Sequence[float], dimensions: int) -> list[float]:
    values = [float(value) for value in vector[:dimensions]]
    if len(values) < dimensions:
        raise EmbeddingError(f"embedding has {len(values)} dimensions; {dimensions} are configured")
    norm = math.sqrt(sum(value * value for value in values))
    if not norm or not math.isfinite(norm):
        raise EmbeddingError("embedding endpoint returned an empty or invalid vector")
    return [value / norm for value in values]


class OpenAICompatibleEmbeddings:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        instruction: str,
        timeout: float = 60,
        retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.instruction = instruction
        self.timeout = timeout
        self.retries = retries

    @property
    def identity(self) -> str:
        return json.dumps([self.base_url, self.model, self.dimensions])

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        output: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            output.extend(self._request(list(texts[start : start + BATCH_SIZE])))
        return output

    def embed_query(self, text: str) -> list[float]:
        # Short timeout and no retries: search falls back to full text instead of hanging.
        return self._request([query_text(self.instruction, text)], timeout=15, retries=0)[0]

    def _request(
        self, texts: list[str], timeout: float | None = None, retries: int | None = None
    ) -> list[list[float]]:
        timeout = self.timeout if timeout is None else timeout
        retries = self.retries if retries is None else retries
        body = json.dumps({"model": self.model, "input": texts}).encode()
        request = Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        payload: dict[str, object] = {}
        for attempt in range(retries + 1):
            try:
                with urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode())
                break
            except HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise EmbeddingError(f"embedding request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise EmbeddingError(f"embedding request failed ({type(exc).__name__})") from None
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("embedding endpoint returned an unexpected response")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        return [truncate_normalize(item["embedding"], self.dimensions) for item in ordered]


def client_from_settings(settings: Settings) -> EmbeddingClient | None:
    if not settings.embeddings_enabled:
        return None
    return OpenAICompatibleEmbeddings(
        settings.embedding_base_url,
        settings.embedding_api_key,
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.query_instruction,
    )
