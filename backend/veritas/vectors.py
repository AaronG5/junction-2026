"""Vector search with Qdrant (docker/qdrant/collections.json, docs/data-architecture.md).

Qdrant stores one 768-number vector per post, claim, category prototype and
cluster centre. Point id = the row's uuid in MySQL. The app always uses the
aliases (posts, claims, categories, clusters); each alias points at a collection
named after its embedding model, so switching models = new collections, then
move the aliases.

Talks to Qdrant's REST API with httpx. Errors become plain sentences:
VectorStoreUnavailable (Qdrant not running) or VectorStoreError (it refused).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import httpx

from .config import ROOT, Settings

COLLECTIONS_FILE = ROOT / "docker" / "qdrant" / "collections.json"
VECTOR_NAME = "text"


class VectorStoreUnavailable(Exception):
    """Qdrant can't be reached. The message is shown to users as is."""


class VectorStoreError(Exception):
    """Qdrant answered with an error. The message is shown to users as is."""


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Point:
    id: str             # the MySQL row's uuid
    vector: Sequence[float]
    payload: dict = field(default_factory=dict)


def load_collections(path: Path = COLLECTIONS_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["collections"]


def _point_id(value: str | uuid.UUID | bytes) -> str:
    if isinstance(value, bytes):
        return str(uuid.UUID(bytes=value))
    return str(uuid.UUID(str(value)))  # normalised; rejects anything that isn't a uuid


class Qdrant:
    """A small client for the parts of Qdrant Veritas Lens uses.

    prefix: put in front of every collection and alias name. Tests use it to
    work in their own throwaway collections; the app leaves it empty."""

    def __init__(self, base_url: str, *, prefix: str = "", timeout: float = 10.0,
                 collections: list[dict] | None = None, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix
        self.collections = collections if collections is not None else load_collections()
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self.dimensions = {c["alias"]: c["config"]["vectors"][VECTOR_NAME]["size"] for c in self.collections}

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> "Qdrant":
        return cls(f"http://127.0.0.1:{settings.qdrant_port}", **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Qdrant":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ plumbing

    def name(self, base: str) -> str:
        return self.prefix + base

    def _request(self, method: str, path: str, body: Any = None, *, allow_404: bool = False) -> Any:
        try:
            res = self._http.request(method, path, json=body)
        except httpx.TransportError:
            raise VectorStoreUnavailable(
                f"Vector search (Qdrant) isn't reachable at {self.base_url}. Start it with the launcher."
            ) from None
        if res.status_code == 404 and allow_404:
            return None
        try:
            data = res.json()
        except ValueError:
            data = {}
        if res.status_code >= 400:
            error = (data.get("status") or {}).get("error") if isinstance(data.get("status"), dict) else None
            raise VectorStoreError(f"Vector search (Qdrant) refused the request: {error or f'HTTP {res.status_code}'}")
        return data.get("result")

    def _check_vector(self, alias: str, vector: Sequence[float]) -> list[float]:
        size = self.dimensions[alias]
        values = [float(v) for v in vector]
        if len(values) != size:
            raise ValueError(f"A vector for '{alias}' must have {size} numbers, not {len(values)}.")
        if any(v != v or v in (float("inf"), float("-inf")) for v in values):
            raise ValueError("A vector can't contain NaN or infinity.")
        return values

    # ------------------------------------------------------------------ setup

    def setup(self) -> list[str]:
        """Create missing collections, payload indexes and aliases. Safe to repeat.
        Returns what was changed (empty when everything was already in place)."""
        changes: list[str] = []
        aliases = self.aliases()
        for spec in self.collections:
            name, alias = self.name(spec["name"]), self.name(spec["alias"])
            info = self._request("GET", f"/collections/{name}", allow_404=True)
            if info is None:
                self._request("PUT", f"/collections/{name}", spec["config"])
                changes.append(f"created collection {name}")
                info = self._request("GET", f"/collections/{name}")
            else:
                have = info["config"]["params"]["vectors"].get(VECTOR_NAME, {})
                want = spec["config"]["vectors"][VECTOR_NAME]
                if have.get("size") != want["size"] or have.get("distance") != want["distance"]:
                    raise VectorStoreError(
                        f"The collection {name} exists but holds {have.get('size')}-number {have.get('distance')} vectors; "
                        f"{want['size']}-number {want['distance']} vectors are expected. "
                        "It was made for another embedding model: create new collections and move the alias."
                    )
            indexed = info.get("payload_schema", {})
            for field_name, schema in spec.get("payload_indexes", {}).items():
                if indexed.get(field_name, {}).get("data_type") != schema:
                    self._request("PUT", f"/collections/{name}/index?wait=true",
                                  {"field_name": field_name, "field_schema": schema})
                    changes.append(f"indexed {name}.{field_name}")
            if aliases.get(alias) != name:
                actions = []
                if alias in aliases:
                    actions.append({"delete_alias": {"alias_name": alias}})
                actions.append({"create_alias": {"collection_name": name, "alias_name": alias}})
                self._request("POST", "/collections/aliases", {"actions": actions})  # one atomic switch
                changes.append(f"alias {alias} -> {name}")
        return changes

    def drop_all(self) -> None:
        """Delete this prefix's aliases and collections (tests only; refuses without a prefix)."""
        if not self.prefix:
            raise ValueError("drop_all() is only for prefixed (test) collections.")
        existing = self.aliases()
        for spec in self.collections:
            alias = self.name(spec["alias"])
            if alias in existing:
                self._request("POST", "/collections/aliases", {"actions": [{"delete_alias": {"alias_name": alias}}]})
            self._request("DELETE", f"/collections/{self.name(spec['name'])}", allow_404=True)

    def aliases(self) -> dict[str, str]:
        return {a["alias_name"]: a["collection_name"]
                for a in (self._request("GET", "/aliases") or {}).get("aliases", [])}

    # ------------------------------------------------------------------ points

    def upsert(self, alias: str, points: Iterable[Point]) -> int:
        body = [{"id": _point_id(p.id), "vector": {VECTOR_NAME: self._check_vector(alias, p.vector)}, "payload": p.payload}
                for p in points]
        if body:
            self._request("PUT", f"/collections/{self.name(alias)}/points?wait=true", {"points": body})
        return len(body)

    def delete(self, alias: str, ids: Iterable[str | uuid.UUID | bytes]) -> None:
        points = [_point_id(i) for i in ids]
        if points:
            self._request("POST", f"/collections/{self.name(alias)}/points/delete?wait=true", {"points": points})

    def count(self, alias: str) -> int:
        return self._request("POST", f"/collections/{self.name(alias)}/points/count", {"exact": True})["count"]

    def ids(self, alias: str, batch: int = 256) -> Iterator[str]:
        """Every point id in a collection, in pages."""
        offset = None
        while True:
            body: dict = {"limit": batch, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            page = self._request("POST", f"/collections/{self.name(alias)}/points/scroll", body)
            for p in page["points"]:
                yield str(p["id"])
            offset = page.get("next_page_offset")
            if offset is None:
                return

    def vectors(self, alias: str, *, flt: dict | None = None, batch: int = 256) -> Iterator[tuple[str, list[float], dict]]:
        """Every point (id, vector, payload) matching the filter, in pages."""
        offset = None
        while True:
            body: dict = {"limit": batch, "with_payload": True, "with_vector": [VECTOR_NAME]}
            if flt:
                body["filter"] = flt
            if offset is not None:
                body["offset"] = offset
            page = self._request("POST", f"/collections/{self.name(alias)}/points/scroll", body)
            for p in page["points"]:
                vector = p.get("vector") or {}
                yield str(p["id"]), list(vector.get(VECTOR_NAME, []) if isinstance(vector, dict) else vector), p.get("payload") or {}
            offset = page.get("next_page_offset")
            if offset is None:
                return

    def set_payload(self, alias: str, ids: Iterable[str | uuid.UUID | bytes], payload: dict) -> None:
        points = [_point_id(i) for i in ids]
        if points:
            self._request("POST", f"/collections/{self.name(alias)}/points/payload?wait=true",
                          {"payload": payload, "points": points})

    def search(self, alias: str, vector: Sequence[float], *, limit: int = 10, flt: dict | None = None,
               min_score: float | None = None) -> list[Hit]:
        body: dict = {"query": self._check_vector(alias, vector), "using": VECTOR_NAME,
                      "limit": max(1, min(int(limit), 1000)), "with_payload": True}
        if flt:
            body["filter"] = flt
        if min_score is not None:
            body["score_threshold"] = min_score
        result = self._request("POST", f"/collections/{self.name(alias)}/points/query", body)
        return [Hit(id=str(p["id"]), score=float(p["score"]), payload=p.get("payload") or {}) for p in result["points"]]

    # ------------------------------------------------------------------ what the app asks

    def similar_posts(self, vector: Sequence[float], *, limit: int = 10, platforms: Sequence[str] = (),
                      languages: Sequence[str] = (), since: str | None = None, until: str | None = None,
                      exclude: Sequence[str | uuid.UUID | bytes] = ()) -> list[Hit]:
        """Posts most similar to `vector`, most similar first.
        platforms/languages: any of these (platform codes like 'telegram', language codes like 'lt').
        since/until: published_at range, ISO 8601 (inclusive). exclude: point ids to leave out (the post itself)."""
        must: list[dict] = []
        if platforms:
            must.append({"key": "platform", "match": {"any": list(platforms)}})
        if languages:
            must.append({"key": "language", "match": {"any": list(languages)}})
        if since or until:
            rng = {k: v for k, v in (("gte", since), ("lte", until)) if v}
            must.append({"key": "published_at", "range": rng})
        flt: dict = {}
        if must:
            flt["must"] = must
        if exclude:
            flt["must_not"] = [{"has_id": [_point_id(i) for i in exclude]}]
        return self.search("posts", vector, limit=limit, flt=flt or None)

    def nearest_cluster(self, vector: Sequence[float], *, min_score: float, run_id: int | None = None) -> Hit | None:
        """The cluster centre closest to `vector`, or None when even the closest is
        less similar than min_score (the post fits no cluster)."""
        flt = {"must": [{"key": "run_id", "match": {"value": run_id}}]} if run_id is not None else None
        hits = self.search("clusters", vector, limit=1, flt=flt, min_score=min_score)
        return hits[0] if hits else None
