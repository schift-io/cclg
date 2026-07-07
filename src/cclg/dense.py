"""Optional dense-retrieval backends.

Dense retrieval is off by default. When enabled it can run fully on-device
(sentence-transformers) or call a hosted embedding API (Schift, OpenAI, Google,
Cloudflare). API backends read their credentials from the environment — never
from config.json — so secrets are not persisted. These backends make paid,
networked calls, so they only run when ``dense.enabled`` is true and a backend is
explicitly selected or auto-detected from present credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from .models import MemoryNode
from .retrieval import SearchHit


# Provider auto-detection precedence and the env vars that activate each one.
PROVIDER_ENV = {
    "schift": ["SCHIFT_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "cloudflare": ["CLOUDFLARE_API_TOKEN"],
}
AUTO_ORDER = ["schift", "openai", "google", "cloudflare", "ollama", "local"]

DEFAULT_MODELS = {
    "schift": "schift-embed",
    "openai": "text-embedding-3-small",
    "google": "text-embedding-004",
    "cloudflare": "@cf/baai/bge-base-en-v1.5",
    "ollama": "nomic-embed-text",
    "local": "ibm-granite/granite-embedding-97m-multilingual-r2",
}


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _http_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - explicit https endpoints
        return json.loads(response.read().decode("utf-8"))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingBackend:
    """Base class: subclasses implement embed(); ranking is shared."""

    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def search(self, query: str, nodes: list[MemoryNode], *, limit: int) -> list[SearchHit]:
        if not query or not nodes:
            return []
        texts = [node.retrieval.get("dense_text") or node.content for node in nodes]
        vectors = self.embed([query, *texts])
        q = vectors[0]
        hits = [SearchHit(node=node, score=_cosine(q, vectors[i + 1]), reasons=[f"dense:{self.name}"]) for i, node in enumerate(nodes)]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


class LocalBackend(EmbeddingBackend):
    name = "local"

    def __init__(self, model: str, *, device: str | None = None) -> None:
        super().__init__(model)
        self._device = device
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("local dense retrieval needs `pip install cclg[dense]`") from exc
            self._model = SentenceTransformer(self.model, device=self._device)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        return [list(map(float, vec)) for vec in model.encode(texts, normalize_embeddings=True)]


class OpenAIBackend(EmbeddingBackend):
    name = "openai"

    def __init__(self, model: str, *, base_url: str | None = None, key_env: tuple[str, ...] = ("OPENAI_API_KEY",), key: str | None = None) -> None:
        super().__init__(model)
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.key = key or _env(*key_env)

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = _http_json(f"{self.base_url}/embeddings", {"model": self.model, "input": texts}, {"Authorization": f"Bearer {self.key}"})
        return [item["embedding"] for item in data["data"]]


class SchiftBackend(OpenAIBackend):
    """Schift exposes an OpenAI-compatible embeddings endpoint."""

    name = "schift"

    def __init__(self, model: str, *, base_url: str | None = None) -> None:
        super().__init__(model, base_url=base_url or _env("SCHIFT_BASE_URL") or "https://api.schift.io/v1", key_env=("SCHIFT_API_KEY",))


class GoogleBackend(EmbeddingBackend):
    name = "google"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.key = _env("GOOGLE_API_KEY", "GEMINI_API_KEY")
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"requests": [{"model": f"models/{self.model}", "content": {"parts": [{"text": text}]}} for text in texts]}
        url = f"{self.base_url}/models/{self.model}:batchEmbedContents?key={self.key}"
        data = _http_json(url, payload, {})
        return [item["values"] for item in data["embeddings"]]


class CloudflareBackend(EmbeddingBackend):
    name = "cloudflare"

    def __init__(self, model: str, *, account_id: str | None = None) -> None:
        super().__init__(model)
        self.key = _env("CLOUDFLARE_API_TOKEN")
        self.account_id = account_id or _env("CLOUDFLARE_ACCOUNT_ID")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.account_id:
            raise RuntimeError("cloudflare dense backend needs CLOUDFLARE_ACCOUNT_ID")
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{self.model}"
        data = _http_json(url, {"text": texts}, {"Authorization": f"Bearer {self.key}"})
        return data["result"]["data"]


class OllamaBackend(EmbeddingBackend):
    """Local Ollama daemon, or any lightweight runtime exposing Ollama's API."""

    name = "ollama"

    def __init__(self, model: str, *, host: str | None = None) -> None:
        super().__init__(model)
        self.host = (host or _env("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = _http_json(f"{self.host}/api/embed", {"model": self.model, "input": texts}, {})
        return data["embeddings"]


_BACKENDS = {
    "local": LocalBackend,
    "ollama": OllamaBackend,
    "openai": OpenAIBackend,
    "schift": SchiftBackend,
    "google": GoogleBackend,
    "cloudflare": CloudflareBackend,
}


def _doc_text(node: MemoryNode) -> str:
    return node.retrieval.get("dense_text") or node.content


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class CachedBackend:
    """Wrap a backend with a persistent per-node embedding cache.

    Document vectors are cached by ``provider:model:node_id`` and invalidated when
    the node's dense text changes (content hash). Only new/changed nodes are sent
    to the backend, which cuts repeat API cost to the query embedding alone.
    """

    def __init__(self, backend: EmbeddingBackend, cache_path: Path) -> None:
        self.backend = backend
        self.cache_path = Path(cache_path)
        self.name = backend.name
        self.model = backend.model

    def _key(self, node_id: str) -> str:
        return f"{self.backend.name}:{self.backend.model}:{node_id}"

    def _load(self) -> dict[str, Any]:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self, cache: dict[str, Any]) -> None:
        from .store import atomic_write_text

        # Atomic so a concurrent warm() cannot tear the embedding cache.
        atomic_write_text(self.cache_path, json.dumps(cache, ensure_ascii=False))

    def warm(self, nodes: list[MemoryNode]) -> int:
        """Embed and cache any uncached/changed nodes. Returns count embedded."""
        cache = self._load()
        pending = [(node, _doc_text(node)) for node in nodes]
        missing = [(node, text) for node, text in pending if cache.get(self._key(node.id), {}).get("hash") != _text_hash(text)]
        if missing:
            vectors = self.backend.embed([text for _, text in missing])
            for (node, text), vec in zip(missing, vectors):
                cache[self._key(node.id)] = {"hash": _text_hash(text), "vec": list(map(float, vec))}
            self._save(cache)
        return len(missing)

    def search(self, query: str, nodes: list[MemoryNode], *, limit: int) -> list[SearchHit]:
        if not query or not nodes:
            return []
        self.warm(nodes)
        cache = self._load()
        q = self.backend.embed([query])[0]
        hits = []
        for node in nodes:
            entry = cache.get(self._key(node.id))
            if not entry:
                continue
            hits.append(SearchHit(node=node, score=_cosine(q, entry["vec"]), reasons=[f"dense:{self.backend.name}"]))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def detect_provider() -> str:
    """Pick a provider from credentials present in the environment."""
    for provider in AUTO_ORDER:
        if provider == "local":
            return "local"
        if provider == "ollama":
            if _env("OLLAMA_HOST"):
                return "ollama"
            continue
        if _env(*PROVIDER_ENV[provider]):
            return provider
    return "local"


def resolve_provider(config: dict | None = None):
    """Return a configured dense backend, or None when dense is disabled.

    Off by default. ``dense.provider`` may be an explicit backend or ``"auto"``
    (detect from env credentials). API backends make paid networked calls, so the
    operator must opt in via ``dense.enabled``.
    """
    dense = (config or {}).get("dense") or {}
    if not dense.get("enabled"):
        return None
    provider = dense.get("provider") or "auto"
    if provider == "auto":
        provider = detect_provider()
    if provider not in _BACKENDS:
        raise ValueError(f"unknown dense provider: {provider}")
    model = dense.get("model") or DEFAULT_MODELS[provider]
    if provider == "local":
        return LocalBackend(model, device=dense.get("device"))
    if provider == "cloudflare":
        return CloudflareBackend(model, account_id=dense.get("account_id"))
    if provider == "schift":
        return SchiftBackend(model, base_url=dense.get("base_url"))
    if provider == "openai":
        return OpenAIBackend(model, base_url=dense.get("base_url"))
    if provider == "ollama":
        return OllamaBackend(model, host=dense.get("host"))
    return _BACKENDS[provider](model)


def provider_status(config: dict | None = None) -> dict[str, Any]:
    dense = (config or {}).get("dense") or {}
    detected = detect_provider()
    available = {p: bool(_env(*envs)) for p, envs in PROVIDER_ENV.items()}
    available["local"] = True
    return {
        "enabled": bool(dense.get("enabled")),
        "configured_provider": dense.get("provider", "auto"),
        "auto_detected": detected,
        "credentials_present": available,
        "model": dense.get("model") or DEFAULT_MODELS.get(dense.get("provider") if dense.get("provider") in DEFAULT_MODELS else detected),
    }
