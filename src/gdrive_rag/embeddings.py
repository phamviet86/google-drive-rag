"""OpenAI-compatible embedding client (default: OpenRouter + qwen/qwen3-embedding-8b).

Vectors are truncated client-side to ``dimensions`` and re-normalized (Matryoshka-style), so the
request never depends on a provider honouring a ``dimensions`` parameter. Only queries get the
Qwen3 instruction prefix ``Instruct: <task>\\nQuery:<query>``; documents are sent as-is.
Adapted from local-rag-mcp ``OpenAIEmbeddings`` (Apache-2.0).
"""

from __future__ import annotations

import http.client
import json
import math
import socket
import ssl
import time
from collections.abc import Sequence
from typing import Protocol

from . import netcheck
from .config import Settings

BATCH_SIZE = 32
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


REJECTED_STATUSES = frozenset({400, 413, 422})


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS to ``host`` (certificate and SNI) over a socket to the address that was checked, so
    a DNS answer that changes after the check cannot redirect the request."""

    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def _post(
    base_url: str, path: str, body: bytes, headers: dict[str, str], timeout: float, allow: bool
) -> tuple[int, bytes]:
    """POST without following redirects (``http.client`` never does), after the address check
    in ``netcheck``; the connection goes to the very address that passed it."""
    scheme, host, port, prefix, address = netcheck.check(base_url, allow)
    target = prefix.rstrip("/") + path
    if scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPS(host, str(address), port, timeout)
    else:
        connection = http.client.HTTPConnection(str(address), port, timeout=timeout)
    try:
        connection.request("POST", target, body=body, headers={**headers, "Host": host})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class EmbeddingError(RuntimeError):
    """Remote embedding call failed; the message never contains the API key.

    ``rejected`` is True when the endpoint refused the input itself (HTTP 400/413/422 or an
    empty vector): the batch should be split to find the bad item. Otherwise (network, sign-in,
    quota, server errors) the whole pass should stop and retry on the next sync.
    """

    def __init__(self, message: str, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


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
        raise EmbeddingError("embedding endpoint returned an empty or invalid vector", True)
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
        allow_local: bool | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # loopback/private endpoints only with GDRAG_ALLOW_LOCAL_EMBEDDINGS (read once here)
        self.allow_local = netcheck.local_allowed() if allow_local is None else allow_local
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
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {}
        for attempt in range(retries + 1):
            try:
                status, raw = _post(
                    self.base_url, "/embeddings", body, headers, timeout, self.allow_local
                )
            except netcheck.DestinationRefused as exc:
                raise EmbeddingError(f"embedding request refused: {exc}") from None
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise EmbeddingError(f"embedding request failed ({type(exc).__name__})") from None
            if status in RETRY_STATUSES and attempt < retries:
                time.sleep(2**attempt)
                continue
            if not 200 <= status < 300:  # a redirect is an error too: it is never followed
                raise EmbeddingError(
                    f"embedding request failed (HTTP {status})", status in REJECTED_STATUSES
                )
            try:
                value = json.loads(raw.decode())
            except ValueError:
                raise EmbeddingError("embedding endpoint returned an unexpected response") from None
            payload = value if isinstance(value, dict) else {}
            break
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
