from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Protocol


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingProvider(Protocol):
    def generate_embedding(self, text: str, *, model: str) -> list[float]:
        ...

    def generate_embeddings(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        ...


class EmbeddingError(RuntimeError):
    pass


def embedding_model() -> str:
    return os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def generate_embedding(text: str, *, model: str | None = None) -> list[float]:
    embeddings = generate_embeddings([text], model=model)
    return embeddings[0] if embeddings else []


def generate_embeddings(texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
    selected_model = model or embedding_model()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EmbeddingError("OPENAI_API_KEY no configurada para generar embeddings.")
    base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    timeout = int(os.getenv("OPENAI_EMBEDDING_TIMEOUT_SECONDS", "8"))
    payload = json.dumps({"model": selected_model, "input": list(texts)}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise EmbeddingError(f"Error HTTP generando embeddings: {exc.code} {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise EmbeddingError(f"No se pudo conectar con el proveedor de embeddings: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
        rows = sorted(parsed.get("data", []), key=lambda item: item.get("index", 0))
        return [[float(value) for value in row["embedding"]] for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError("Respuesta de embeddings inválida.") from exc


class OpenAIEmbeddingProvider:
    def generate_embedding(self, text: str, *, model: str) -> list[float]:
        return generate_embedding(text, model=model)

    def generate_embeddings(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return generate_embeddings(texts, model=model)

