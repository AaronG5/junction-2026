"""Where the fact check looks for evidence: the "search tool" Gemma's claims are checked with.

The post itself is never evidence, and neither is anything from the propaganda side of the
project: data/parsed (pro-Kremlin Telegram channels, Respublika, Būkime vieningi) and the
posts in the database are never searched here. Two providers today:

  TrustedFiles   keyword search ("grep") over the documents in data/evidence/ (EVIDENCE_DIR):
                 fact-checks, encyclopedia extracts, articles from outlets you trust. Formats:
                   *.jsonl / *.json   one document per line / a list: {"title", "text", "url", "source", "date"}
                   *.md / *.txt       one document per file (title = first line)
                 A document from a known propaganda domain is refused even if it is put there.
  DisinfoBank    the narrative bank's documented disinformation claims (EUvsDisinfo, Antibot4Navalny,
                 Operation Overload): a claim that closely matches one of them is evidence that it is a
                 known false narrative.

More can plug in with the same search(claim, queries) -> [Source] method, for example a web-search
model such as Gemini with Google Search grounding.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

log = logging.getLogger("veritas.evidence")

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = ROOT / "data" / "evidence"
SNIPPET = 320          # characters of text around the best hit
DISINFO_MIN = 0.85     # cosine for "this claim is a documented disinformation claim"

# Never evidence, wherever a document comes from.
UNTRUSTED_DOMAINS = ("t.me", "telegram.me", "respublika.lt", "bukimevieningi.lt", "rt.com", "sputniknews",
                     "sputnik", "ria.ru", "tass.ru", "tass.com", "rt.rs", "baltnews", "news-front", "rubaltic",
                     "pravda-en.com", "pravda.ru")
_STOP = set("""a an and are as at be but by for from has have in is it its of on or that the this to was were will with
ir ar bei kad su per nuo iki už yra buvo bus tai tas ta jo jos jų mes jūs jie jos kaip""".split())


@dataclass
class Source:
    title: str
    text: str            # the part that matched
    source: str          # who published it, e.g. "LRT", "EUvsDisinfo"
    url: str | None = None
    date: str | None = None
    kind: str = "document"   # "document" (trusted file) or "disinfo" (documented disinformation claim)
    score: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    def search(self, claim: str, queries: Sequence[str], limit: int = 4) -> list[Source]: ...


def fold(text: str) -> str:
    """Lower case without accents, so "Lietuvą" finds "lietuva"."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def terms(text: str) -> list[str]:
    seen, out = set(), []
    for w in re.findall(r"[\w-]+", fold(text)):
        if len(w) >= 3 and w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def untrusted(url: str | None, source: str | None) -> bool:
    hay = f"{url or ''} {source or ''}".lower()
    return any(d in hay for d in UNTRUSTED_DOMAINS)


# --------------------------------------------------------------------------- trusted documents (grep)

@dataclass
class _Doc:
    title: str
    text: str
    folded: str
    source: str
    url: str | None
    date: str | None


class TrustedFiles:
    """Keyword search over data/evidence/. Re-reads the folder when a file changes."""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder or os.environ.get("EVIDENCE_DIR") or EVIDENCE_DIR)
        self._docs: list[_Doc] = []
        self._stamp: tuple = ()
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.rglob("*") if p.suffix.lower() in (".jsonl", ".json", ".md", ".txt")
                      and p.name.lower() != "readme.md")

    def _load(self) -> list[_Doc]:
        files = self._files()
        stamp = tuple((str(p), p.stat().st_mtime) for p in files)
        with self._lock:
            if stamp == self._stamp:
                return self._docs
            docs: list[_Doc] = []
            for p in files:
                try:
                    docs.extend(self._read(p))
                except (OSError, ValueError) as exc:
                    log.warning("evidence file %s skipped: %s", p, exc)
            self._docs, self._stamp = docs, stamp
            log.info("evidence: %d trusted documents in %s", len(docs), self.folder)
            return docs

    def _read(self, p: Path) -> list[_Doc]:
        raw: list[dict] = []
        if p.suffix.lower() == ".jsonl":
            raw = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        elif p.suffix.lower() == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data if isinstance(data, list) else data.get("documents", []) if isinstance(data, dict) else []
        else:
            text = p.read_text(encoding="utf-8")
            first = text.strip().splitlines()[0] if text.strip() else p.stem
            raw = [{"title": first.lstrip("# ").strip(), "text": text, "source": p.parent.name if p.parent != self.folder else p.stem}]
        out = []
        for d in raw:
            if not isinstance(d, dict) or not (d.get("text") or "").strip():
                continue
            if untrusted(d.get("url"), d.get("source")):
                log.warning("evidence: %s comes from a propaganda source and is ignored", d.get("url") or d.get("source"))
                continue
            text = str(d["text"])
            out.append(_Doc(str(d.get("title") or p.stem), text, fold(text), str(d.get("source") or p.stem),
                            d.get("url"), d.get("date")))
        return out

    def search(self, claim: str, queries: Sequence[str], limit: int = 4) -> list[Source]:
        docs = self._load()
        if not docs:
            return []
        wanted = terms(" ".join([claim, *queries]))
        if not wanted:
            return []
        scored = []
        for d in docs:
            hits = [t for t in wanted if t in d.folded]
            # Enough of the claim's words must be there: one shared word is not evidence.
            if len(hits) < max(2, min(4, len(wanted) // 3)):
                continue
            scored.append((len(hits) / len(wanted), hits, d))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, hits, d in scored[:limit]:
            out.append(Source(title=d.title, text=_snippet(d.text, d.folded, hits), source=d.source, url=d.url,
                              date=d.date, kind="document", score=round(score, 3)))
        return out


def _snippet(text: str, folded: str, hits: list[str]) -> str:
    """The SNIPPET characters around the densest cluster of hits."""
    positions = sorted(i for t in hits for i in [folded.find(t)] if i >= 0)
    if not positions:
        return text[:SNIPPET]
    best, best_n = positions[0], 0
    for p in positions:
        n = sum(1 for q in positions if p <= q < p + SNIPPET)
        if n > best_n:
            best, best_n = p, n
    start = max(0, best - 60)
    piece = text[start:start + SNIPPET].strip()
    return ("…" if start > 0 else "") + " ".join(piece.split()) + ("…" if start + SNIPPET < len(text) else "")


# --------------------------------------------------------------------------- documented disinformation

class DisinfoBank:
    """The narrative bank's verdict claims (documented disinformation), matched by meaning."""

    def __init__(self, bank_getter: Callable[[], object], embed: Callable[[Sequence[str]], list[list[float]]]):
        self._bank, self._embed = bank_getter, embed
        self._vectors: dict[str, list[float]] = {}

    def prepare(self, claims: Sequence[str]) -> None:
        """Embed all of a post's claims in one call (the local AI swaps models between embedding
        and chat, so one call instead of one per claim saves a lot of time)."""
        todo = [c for c in dict.fromkeys(claims) if c not in self._vectors]
        if not todo:
            return
        try:
            for c, v in zip(todo, self._embed(todo)):
                self._vectors[c] = v
        except Exception as exc:
            log.info("evidence: embedding claims failed (%s)", exc)

    def search(self, claim: str, queries: Sequence[str], limit: int = 2) -> list[Source]:
        try:
            import numpy as np
            bank = self._bank()
            emb, rows = getattr(bank, "claim_emb", None), getattr(bank, "claims", None)
            if emb is None or not rows:
                return []
            v = np.asarray(self._vectors.get(claim) or self._embed([claim])[0], np.float32)
            v /= max(float(np.linalg.norm(v)), 1e-9)
            sims = emb @ v
        except Exception as exc:  # no bank, no numpy, AI down: this provider just finds nothing
            log.info("evidence: disinformation bank skipped (%s)", exc)
            return []
        out, seen = [], set()
        for j in np.argsort(-sims)[: limit * 4]:
            s, r = float(sims[j]), rows[j]
            if s < DISINFO_MIN or r.get("narrative_id") in seen:
                continue
            seen.add(r.get("narrative_id"))
            name = {"euvsdisinfo": "EUvsDisinfo", "a4n": "Antibot4Navalny", "overload": "Operation Overload"}.get(
                str(r.get("source") or "").lower(), r.get("source") or "a disinformation database")
            out.append(Source(title=f"Documented disinformation claim ({name})", text=str(r.get("text") or ""),
                              source=name, url=r.get("url") if str(r.get("url") or "").startswith("https://") else None,
                              date=r.get("date") or r.get("date_first"), kind="disinfo", score=round(s, 3)))
            if len(out) == limit:
                break
        return out


def search_all(providers: Sequence[Provider], claim: str, queries: Sequence[str], limit: int = 5) -> list[Source]:
    """Every provider's sources for one claim, documented disinformation first, at most `limit`."""
    found: list[Source] = []
    for p in providers:
        try:
            found.extend(p.search(claim, queries))
        except Exception as exc:
            log.warning("evidence provider %s failed: %s", type(p).__name__, exc)
    found = [s for s in found if not untrusted(s.url, s.source)]
    found.sort(key=lambda s: (s.kind != "disinfo", -s.score))
    return found[:limit]
