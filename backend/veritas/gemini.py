"""Google's Gemini API, for the work where quality matters more than privacy and speed:
claim extraction and fact checks with Google Search (veritas/factcheck.py).

- The key comes from .env (GEMINI_API_KEY) and is sent only in a request header; it never
  appears in errors, logs or reprs. The model is GEMINI_MODEL (default gemini-flash-latest: cheap, and good enough for claims and checks).
- Answers are JSON fitting a schema (pydantic), like the local AI's (veritas/ai.py), so
  prompts and checks work the same way. An unusable answer is retried; so are rate limits
  and server errors (with waits), because speed is not the priority here.
- search=True lets Gemini use Google Search; the sources it used come back with the answer
  (title, link, and the sentences of the answer each one backs).
- Never used when Providence is set to work only on this computer (data mode "local"):
  see available().
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .ai import Prompt

log = logging.getLogger("veritas.gemini")

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-latest"
RETRY_WAITS = (5, 20, 60)          # seconds before retrying a rate limit or a server error
T = TypeVar("T", bound=BaseModel)


class GeminiUnavailable(Exception):
    """Gemini can't be reached or refuses the key. Shown to users as is."""


class GeminiError(Exception):
    """Gemini answered with something unusable. Shown to users as is."""


@dataclass(frozen=True)
class WebSource:
    title: str            # usually the site's domain, e.g. "lrt.lt"
    url: str              # Google's redirect link to the page (resolve() gives the page itself)
    backs: tuple[str, ...] = ()   # sentences of the answer this source backs


@dataclass(frozen=True)
class GeminiAnswer(Generic[T]):
    value: T
    model: str            # the model that answered, e.g. "gemini-3.1-pro-preview"
    prompt: str           # "claims_strict.v1"
    sources: tuple[WebSource, ...] = field(default=())
    searches: tuple[str, ...] = field(default=())


def inline_schema(schema: dict) -> dict:
    """pydantic's JSON schema with every $ref replaced by its definition (and no titles)."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class Gemini:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, timeout: float = 300.0,
                 transport: httpx.BaseTransport | None = None, waits: tuple[float, ...] = RETRY_WAITS):
        if not api_key:
            raise GeminiUnavailable("Gemini isn't set up: add GEMINI_API_KEY to the .env file.")
        self.model = model or DEFAULT_MODEL
        self.waits = waits
        self._http = httpx.Client(base_url=API, timeout=timeout, transport=transport,
                                  headers={"x-goog-api-key": api_key, "content-type": "application/json"})

    def __repr__(self) -> str:  # never the key
        return f"Gemini(model={self.model!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Gemini":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, body: dict) -> dict:
        for attempt in range(len(self.waits) + 1):
            try:
                res = self._http.post(f"/models/{self.model}:generateContent", json=body)
            except httpx.TimeoutException:
                problem = "Gemini took too long to answer."
            except httpx.TransportError:
                problem = "Gemini can't be reached. Check the internet connection."
            else:
                if res.status_code < 400:
                    try:
                        data = res.json()
                    except ValueError:
                        raise GeminiError("Gemini sent an answer that isn't JSON.") from None
                    if not isinstance(data, dict):
                        raise GeminiError("Gemini sent an answer in an unexpected shape.")
                    return data
                if res.status_code in (401, 403):
                    raise GeminiUnavailable("Gemini refused the API key (GEMINI_API_KEY in .env). Check that it is valid.")
                if res.status_code == 404:
                    raise GeminiUnavailable(f"Gemini has no model called {self.model!r} (GEMINI_MODEL in .env).")
                if res.status_code not in (429, 500, 502, 503, 504):
                    raise GeminiError(f"Gemini refused the request (error {res.status_code}).")
                problem = ("Gemini's usage limit was reached." if res.status_code == 429
                           else f"Gemini had a problem (error {res.status_code}).")
            if attempt < len(self.waits):
                log.warning("%s Trying again in %s s.", problem, self.waits[attempt])
                time.sleep(self.waits[attempt])
        raise GeminiUnavailable(f"{problem} Try again later.")

    def ask(self, prompt: Prompt, schema: type[T], *, search: bool = False, **values: str) -> GeminiAnswer[T]:
        """Ask Gemini; the answer must be JSON fitting `schema`. Retried once when unusable."""
        system, user = prompt.fill(**values)
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseJsonSchema": inline_schema(schema.model_json_schema())},
        }
        if search:
            body["tools"] = [{"google_search": {}}]
        for _attempt in range(2):
            data = self._post(body)
            try:
                candidate = data["candidates"][0]
                text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
                value = schema.model_validate(json.loads(text))
            except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                continue
            sources, searches = _grounding(candidate.get("groundingMetadata") or {})
            return GeminiAnswer(value=value, model=str(data.get("modelVersion") or self.model), prompt=prompt.label,
                                sources=sources, searches=searches)
        raise GeminiError(f"Gemini's answer to {prompt.label} couldn't be read, twice in a row.")

    def resolve(self, url: str) -> str:
        """The page behind one of Google's grounding redirect links (the link itself when that fails)."""
        if "grounding-api-redirect" not in url:
            return url
        try:
            res = httpx.head(url, follow_redirects=False, timeout=15)
            location = res.headers.get("location", "")
            return location if location.startswith(("https://", "http://")) else url
        except httpx.HTTPError:
            return url


def _grounding(meta: dict) -> tuple[tuple[WebSource, ...], tuple[str, ...]]:
    chunks = [c.get("web") or {} for c in meta.get("groundingChunks") or []]
    backs: dict[int, list[str]] = {}
    for support in meta.get("groundingSupports") or []:
        sentence = ((support.get("segment") or {}).get("text") or "").strip()
        for i in support.get("groundingChunkIndices") or []:
            if sentence and sentence not in backs.setdefault(i, []):
                backs[i].append(sentence)
    sources = tuple(WebSource(title=str(c.get("title") or c.get("domain") or "Web page"), url=str(c["uri"]),
                              backs=tuple(backs.get(i, ())))
                    for i, c in enumerate(chunks) if c.get("uri"))
    return sources, tuple(str(q) for q in meta.get("webSearchQueries") or [])
