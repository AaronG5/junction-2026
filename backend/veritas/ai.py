"""The local AI: llama-swap's OpenAI-style API on this computer (Local_AI_Plan.MD).

Two models, by their llama-swap aliases:
  embedding  EmbeddingGemma: 768 numbers per text. Stored posts and search queries
             use different prompt templates (the same as embedding_models in init.sql).
  chat       Gemma: answers as JSON that must fit a schema (pydantic model).
             Its "thinking" is switched off: answers come faster and never get cut
             off half-way through hidden reasoning.

Prompts are versioned files in backend/prompts/ (claims.v1.md, ...). Every result
says which prompt version and model produced it.

Errors are plain sentences: AiUnavailable (not running, not ready, too slow) and
AiError (an answer that couldn't be used).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Sequence, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import ROOT, Settings

PROMPTS_DIR = ROOT / "backend" / "prompts"
EMBEDDING_DIMENSIONS = 768
# EmbeddingGemma's task prompts; must equal embedding_models row 1 in init.sql.
DOCUMENT_PROMPT = "title: {title} | text: {text}"
QUERY_PROMPT = "task: search result | query: {text}"

MAX_TOKENS_CEILING = 8192

_PROMPT_FILE = re.compile(r"^([a-z_]+)\.v([0-9]+)\.md$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

T = TypeVar("T", bound=BaseModel)


class AiUnavailable(Exception):
    """The local AI isn't running, isn't ready, or is too slow. Shown to users as is."""


class AiError(Exception):
    """The local AI answered with something unusable. Shown to users as is."""


# --------------------------------------------------------------------------- prompts

@dataclass(frozen=True)
class Prompt:
    name: str       # "claims"
    version: str    # "v1"
    system: str
    user: str

    @property
    def label(self) -> str:
        return f"{self.name}.{self.version}"

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.system)) | set(_PLACEHOLDER.findall(self.user))

    def fill(self, **values: str) -> tuple[str, str]:
        """System and user text with {placeholders} filled in. Done in one pass, so
        braces inside the values (a post can contain "{text}") are kept as they are."""
        missing = self.placeholders() - values.keys()
        if missing:
            raise ValueError(f"Prompt {self.label} needs a value for: {', '.join(sorted(missing))}")
        sub = lambda text: _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)  # noqa: E731
        return sub(self.system), sub(self.user)


def _section(text: str, name: str, path: Path) -> str:
    m = re.search(rf"^## {name}[ \t]*\r?\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        raise ValueError(f"{path.name} has no '## {name}' section.")
    return m.group(1).strip()


def load_prompt(name: str, version: str | None = None, folder: Path = PROMPTS_DIR) -> Prompt:
    """A prompt file: the newest version unless one is asked for."""
    found = {}
    for path in folder.glob(f"{name}.v*.md"):
        m = _PROMPT_FILE.match(path.name)
        if m and m.group(1) == name:
            found[int(m.group(2))] = path
    if not found:
        raise ValueError(f"There is no prompt called {name!r} in {folder}.")
    number = int(version.lstrip("v")) if version else max(found)
    if number not in found:
        raise ValueError(f"Prompt {name!r} has no version v{number}.")
    path = found[number]
    text = path.read_text(encoding="utf-8")
    return Prompt(name, f"v{number}", _section(text, "system", path), _section(text, "user", path))


# --------------------------------------------------------------------------- client

def _finish_reason(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def grammar_schema(schema: dict) -> dict:
    """The JSON schema as sent to the model: without maxLength. llama.cpp turns the
    schema into a grammar, and a long maxLength (e.g. 20000) makes one too big to
    build. Lengths are still checked when the answer is read (pydantic)."""
    if isinstance(schema, dict):
        return {k: grammar_schema(v) for k, v in schema.items() if k != "maxLength"}
    if isinstance(schema, list):
        return [grammar_schema(v) for v in schema]
    return schema


@dataclass(frozen=True)
class Answer(Generic[T]):
    value: T
    model: str          # what llama-swap says answered, e.g. "chat" -> recorded with the result
    prompt: str         # "claims.v1"


class LocalAI:
    def __init__(self, base_url: str, *, timeout: float = 120.0, chat_model: str = "chat",
                 embedding_model: str = "embedding", transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> "LocalAI":
        return cls(f"http://127.0.0.1:{settings.llama_swap_port}/v1", **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LocalAI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def check(self, timeout: float = 5.0) -> None:
        """Quick check that llama-swap is running (before starting a long job)."""
        try:
            res = self._http.get("/models", timeout=timeout)
        except httpx.HTTPError:
            raise AiUnavailable("The local AI isn't running. Start it with the launcher.") from None
        if res.status_code >= 400:
            raise AiUnavailable(f"The local AI isn't ready (it answered with error {res.status_code}). "
                                "It may still be loading; try again in a minute.")

    def model_name(self, alias: str) -> str:
        """The real model behind a llama-swap alias ("chat" -> "gemma-4-E2B-it"), for the record.
        Falls back to the alias when llama-swap doesn't say."""
        cache = self.__dict__.setdefault("_model_names", {})
        if alias not in cache:
            try:
                res = self._http.get("/models")
                for m in res.json().get("data", []):
                    if m.get("id") == alias:
                        cache[alias] = ((m.get("meta") or {}).get("llamaswap") or {}).get("modelID") or alias
                        break
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            cache.setdefault(alias, alias)
        return cache[alias]

    def _post(self, path: str, body: dict) -> dict:
        try:
            res = self._http.post(path, json=body)
        except httpx.TimeoutException:
            raise AiUnavailable("The local AI took too long to answer. Try again, or use a smaller model.") from None
        except httpx.TransportError:
            raise AiUnavailable("The local AI isn't running. Start it with the launcher.") from None
        if res.status_code >= 500:
            raise AiUnavailable(f"The local AI isn't ready (it answered with error {res.status_code}). "
                                "It may still be loading; try again in a minute.")
        if res.status_code >= 400:
            raise AiError(f"The local AI refused the request (error {res.status_code}).")
        try:
            data = res.json()
        except ValueError:
            raise AiError("The local AI sent an answer that isn't JSON.") from None
        if not isinstance(data, dict):
            raise AiError("The local AI sent an answer in an unexpected shape.")
        return data

    # ------------------------------------------------------------------ embeddings

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        data = self._post("/embeddings", {"model": self.embedding_model, "input": inputs})
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            vectors = [[float(x) for x in r["embedding"]] for r in rows]
        except (KeyError, TypeError, ValueError):
            raise AiError("The local AI sent embeddings in an unexpected shape.") from None
        if len(vectors) != len(inputs):
            raise AiError(f"The local AI sent {len(vectors)} embeddings for {len(inputs)} texts.")
        for v in vectors:
            if len(v) != EMBEDDING_DIMENSIONS:
                raise AiError(f"The embedding model gave {len(v)} numbers instead of {EMBEDDING_DIMENSIONS}. "
                              "Check which model the launcher loaded.")
            if not all(math.isfinite(x) for x in v):
                raise AiError("The embedding model gave an invalid vector.")
        return vectors

    def embed_documents(self, texts: Sequence[str], titles: Sequence[str | None] | None = None) -> list[list[float]]:
        """Vectors for posts to store (EmbeddingGemma's document prompt; no title = "none")."""
        titles = list(titles) if titles is not None else [None] * len(texts)
        if len(titles) != len(texts):
            raise ValueError("Give one title (or None) per text.")
        inputs = [DOCUMENT_PROMPT.replace("{title}", (t or "none").strip() or "none").replace("{text}", x)
                  for t, x in zip(titles, texts)]
        return self._embed(inputs)

    def embed_query(self, text: str) -> list[float]:
        """A vector for searching (EmbeddingGemma's query prompt)."""
        return self._embed([QUERY_PROMPT.replace("{text}", text)])[0]

    # ------------------------------------------------------------------ chat with a JSON answer

    def ask(self, prompt: Prompt, schema: type[T], *, max_tokens: int = 1024, **values: str) -> Answer[T]:
        """Ask the chat model; the answer must be JSON fitting `schema`.
        An unusable answer is retried once; a second one raises AiError."""
        system, user = prompt.fill(**values)
        body = {
            "model": self.chat_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": prompt.name, "schema": grammar_schema(schema.model_json_schema()),
                                                "strict": True}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        for _attempt in range(2):
            data = self._post("/chat/completions", body)
            try:
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                value = schema.model_validate(json.loads(content))
            except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                # Cut off (often while Gemma still "thought", which it sometimes does even
                # when asked not to): the retry gets a much bigger budget.
                if _finish_reason(data) == "length":
                    body["max_tokens"] = min(body["max_tokens"] * 4, MAX_TOKENS_CEILING)
                continue
            return Answer(value=value, model=self.model_name(self.chat_model), prompt=prompt.label)
        raise AiError(f"The local AI's answer to {prompt.label} couldn't be read, twice in a row.")
