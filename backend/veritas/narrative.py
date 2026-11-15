"""Narrative check for single posts (browser extension + dashboard), from the Decode the Noise pipeline.

Fast path (here, CPU, about 0.1-0.3 s per batch on top of embedding):
  1. embed the post with the backend's own EmbeddingGemma (LocalAI.embed_documents, 768-d), so the vectors live
     in the same space as Qdrant;
  2. source match: nearest post in a bank of public pro-Kremlin Telegram posts. A whole-post cosine at or above
     `copy_strong` means the post copies (or translates) one of them;
  3. candidate claims: the top-k distinct narratives from the claim bank (EUvsDisinfo + Overload + Antibot4Navalny);
  4. state-media echo: the nearest RT / RIA narratives, shown as context only (never decides the verdict);
  5. coordination: the same text (cosine >= copy_strong) posted by other accounts in the last `window_s`
     seconds, and authors that keep copying Telegram posts.
Slow path (background thread): the fine-tuned Gemma judge on Modal decides whether the post *asserts* one of the
candidate claims (reporting, quoting to reject, debunking or sharing the topic is not asserting). Only posts that
pass a similarity gate go to the judge.

Bank layout (`NARRATIVE_BANK_DIR`, built by propaganda/gemma in the Decode the Noise repo):
  manifest.json                     {"dim": 768, "prompt": "document", "thresholds": {...}, "verdict_shards": [...],
                                     "echo_shards": [...]}
  claims/<shard>/claims.jsonl       one claim per line: {"claim_id", "narrative_id", "text", "source", ...}
  claims/<shard>/emb.npy            float16/32 [n, 768], L2-normalised, same row order
  sources/telegram/meta.jsonl       {"channel", "url", "date", "text"} per row
  sources/telegram/emb.npy
Missing parts are skipped; the endpoints still answer (with fewer fields).

Defaults are built in (JUDGE_URL and BANK_DIR below). Optional overrides: NARRATIVE_BANK_DIR, NARRATIVE_JUDGE_URL ("off" = no judge),
NARRATIVE_JUDGE_WORKERS (default 4), NARRATIVE_FEEDBACK (default data/narrative_feedback.jsonl).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import httpx
import numpy as np

log = logging.getLogger("veritas.narrative")
ROOT = Path(__file__).resolve().parents[2]
# The fine-tuned Gemma judge (Modal). Env NARRATIVE_JUDGE_URL overrides it; "off" disables the judge.
JUDGE_URL = "https://augustasrink--ppg-playground-playground-web.us-east.modal.run"
BANK_DIR = ROOT / "data" / "narrative_bank"

DEFAULT_THRESHOLDS = {
    "copy_strong": 0.95,     # whole-post cosine: copy/translation of a bank post (calibrated on the 256-d model;
                             # the manifest overrides it with the value calibrated for the 768-d model)
    "candidate_gate": 0.80,  # send to the judge only if the best candidate claim is at least this close ...
    "echo_min": 0.80,        # show a state-media echo only above this
    "author_repeat": 3,      # an author needs at least this many strong source matches ...
    "author_share": 0.2,     # ... and they must be at least this share of the author's posts we have seen. Share is
                             # the main test: high-volume news accounts and officials post the same events as
                             # Telegram channels a few times, a relay bot does it constantly (10k-post sweep).
    "copy_accounts": 3,      # this many other accounts posting the same text -> coordinated
}
TECHNIQUES = {"LL": "loaded language", "NC": "name-calling", "DO": "doubt", "QR": "questioning reputation",
              "EM": "exaggeration/minimisation", "AF": "appeal to fear", "RE": "repetition",
              "CK": "conversation killer", "AH": "appeal to hypocrisy", "AA": "appeal to authority",
              "FW": "flag-waving", "SL": "slogan", "AV": "appeal to values", "GA": "guilt by association",
              "CO": "causal oversimplification", "CQ": "consequential oversimplification", "FD": "false dilemma",
              "OV": "obfuscation", "AP": "appeal to popularity", "SM": "straw man", "RH": "red herring",
              "AT": "appeal to time", "WA": "whataboutism"}
ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
MAX_TEXT = 1500


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", text or "")).strip()[:MAX_TEXT]


_SENT = re.compile(r"(?<=[.!?…。！？])\s+|\n+")


def query_units(raw: str) -> list[str]:
    """Retrieval units for one post (same rule as propaganda/gemma/closeness.query_chunks): unit 0 = the whole post
    (cleaned), then sliding windows of 2 and 3 sentences (stride 1). Single sentences only when the post has <= 2
    sentences: alone they lose context and one lexical sentence used to fill every candidate slot."""
    whole = clean(raw)
    if not whole:
        return []
    sents = [x.strip() for x in _SENT.split(re.sub(r"[ \t]+", " ", raw or "").strip()) if len(x.strip()) >= 15]
    units = [" ".join(sents[i:i + w]) for w in (2, 3) for i in range(len(sents) - w + 1)]
    if len(sents) <= 2:
        units += sents
    out = [whole]
    for u in units:
        u = clean(u)
        if u and u not in out:
            out.append(u)
    return out[:12]


def _load_matrix(path: Path) -> np.ndarray:
    m = np.load(path).astype(np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


def _jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class Bank:
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    claims: list[dict] = field(default_factory=list)       # verdict shards
    claim_emb: np.ndarray | None = None
    echo: list[dict] = field(default_factory=list)         # state-media shards (context only)
    echo_emb: np.ndarray | None = None
    tg: list[dict] = field(default_factory=list)
    tg_emb: np.ndarray | None = None
    manifest: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "Bank":
        b = cls()
        if not root.is_dir():
            log.warning("narrative bank %s not found; source match and claims are off. "
                        "Download it with: uv run python backend/fetch_narrative_bank.py", root)
            return b
        man = json.loads((root / "manifest.json").read_text()) if (root / "manifest.json").is_file() else {}
        b.manifest = man
        b.thresholds.update(man.get("thresholds", {}))
        verdict = set(man.get("verdict_shards", ["euvsdisinfo", "a4n", "overload"]))
        echo = set(man.get("echo_shards", ["rt", "ria"]))
        vc, ve, ec, ee = [], [], [], []
        for d in sorted((root / "claims").glob("*")) if (root / "claims").is_dir() else []:
            if not (d / "emb.npy").is_file():
                continue
            rows, emb = _jsonl(d / "claims.jsonl"), _load_matrix(d / "emb.npy")
            assert len(rows) == len(emb), f"{d}: {len(rows)} claims vs {len(emb)} vectors"
            for r in rows:
                r.setdefault("source", d.name)
            if d.name in verdict:
                vc += rows; ve.append(emb)
            elif d.name in echo:
                ec += rows; ee.append(emb)
        b.claims, b.claim_emb = vc, (np.concatenate(ve) if ve else None)
        b.echo, b.echo_emb = ec, (np.concatenate(ee) if ee else None)
        tg = root / "sources" / "telegram"
        if (tg / "emb.npy").is_file():
            b.tg, b.tg_emb = _jsonl(tg / "meta.jsonl"), _load_matrix(tg / "emb.npy")
            assert len(b.tg) == len(b.tg_emb)
        log.info("narrative bank: %d claims, %d echo, %d telegram", len(b.claims), len(b.echo), len(b.tg))
        return b


class RecentPosts:
    """Vectors of recently seen posts, for 'same text from other accounts' (bounded ring, time window)."""

    def __init__(self, dim: int = 768, size: int = 20000, window_s: int = 3600):
        self.vec = np.zeros((size, dim), np.float32)
        self.meta: list[tuple[str, str, float] | None] = [None] * size   # (id, author, time)
        self.i, self.size, self.window_s, self.lock = 0, size, window_s, threading.Lock()

    def add_and_match(self, pid: str, author: str, v: np.ndarray, thr: float, now: float) -> set[str]:
        with self.lock:
            sims = self.vec @ v
            hits = set()
            for j in np.nonzero(sims >= thr)[0]:
                m = self.meta[j]
                if m and m[0] != pid and m[1] != author and now - m[2] <= self.window_s:
                    hits.add(m[1])
            if self.vec.shape[1] == len(v):
                self.vec[self.i], self.meta[self.i] = v, (pid, author, now)
                self.i = (self.i + 1) % self.size
            return hits


class Engine:
    def __init__(self, bank: Bank, embed: Callable[[Sequence[str]], list[list[float]]],
                 judge: Callable[[str, list[str]], dict] | None, workers: int = 4,
                 feedback_path: Path | None = None, clock: Callable[[], float] = time.time):
        self.bank, self.embed, self.judge, self.clock = bank, embed, judge, clock
        self.t = bank.thresholds
        self.results: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="judge") if judge else None
        self.recent = RecentPosts()
        self.author_hits: dict[str, int] = defaultdict(int)
        self.author_seen: dict[str, int] = defaultdict(int)
        self.feedback_path = feedback_path

    # ---------------------------------------------------------------- fast path
    def _top(self, emb: np.ndarray | None, rows: list[dict], v: np.ndarray, k: int, distinct: str | None = None):
        if emb is None or not len(rows):
            return []
        s = emb @ v
        out, seen = [], set()
        for j in np.argsort(-s)[: k * 6]:
            key = rows[j].get(distinct) if distinct else j
            if distinct and key in seen:
                continue
            seen.add(key)
            out.append((float(s[j]), rows[j]))
            if len(out) == k:
                break
        return out

    def _union(self, emb: np.ndarray | None, rows: list[dict], U: np.ndarray, k: int, reserve: int = 2,
               per_unit: int = 3):
        """Candidates over all units of a post. U: [units, dim], row 0 = whole post. The first `reserve` slots go to
        the whole post's top distinct narratives, the rest to the windows' merged list (each window's top
        `per_unit`, max sim per narrative). Test v2 recall@5: 0.924 vs 0.793 whole post only / 0.859 best chunk."""
        if emb is None or not len(rows):
            return []
        whole = self._top(emb, rows, U[0], k + reserve + 3, distinct="narrative_id")
        merged: dict = {}
        for u in range(1, len(U)):
            for s, r in self._top(emb, rows, U[u], per_unit, distinct="narrative_id"):
                key = r.get("narrative_id")
                if key not in merged or s > merged[key][0]:
                    merged[key] = (s, r)
        out, seen = [], set()
        for s, r in whole[:reserve] + sorted(merged.values(), key=lambda x: -x[0]) + whole[reserve:]:
            key = r.get("narrative_id")
            if key in seen:
                continue
            seen.add(key)
            out.append((s, r))
            if len(out) == k:
                break
        return sorted(out, key=lambda x: -x[0])

    def batch(self, items: list[dict]) -> list[dict]:
        now = self.clock()
        todo, out = [], {}
        for it in items:
            pid = str(it.get("id", ""))
            if not ID_RE.match(pid):
                out[pid] = {"id": pid, "status": "error", "error": "bad id"}
                continue
            with self.lock:
                if pid in self.results:
                    out[pid] = self.results[pid]
                    continue
            text = clean(it.get("text", ""))
            if len(text) < 20:
                out[pid] = self._store(pid, {"id": pid, "status": "done", "content": _none_content(), "source_match": None,
                                              "coordination": None})
                continue
            todo.append((pid, str(it.get("author") or ""), text, query_units(it.get("text", "")) or [text]))
        if todo:
            try:   # one embedding call for every unit of every post; unit 0 of each post = the whole post
                flat = [u for *_, us in todo for u in us]
                allv = np.asarray(self.embed(flat), np.float32)
                allv /= np.maximum(np.linalg.norm(allv, axis=1, keepdims=True), 1e-9)
                vecs, i = [], 0
                for *_, us in todo:
                    vecs.append(allv[i:i + len(us)])
                    i += len(us)
            except Exception as exc:  # the local AI is down: answer every item, don't fail the batch
                log.warning("embedding failed: %s", exc)
                for pid, *_ in todo:
                    out[pid] = {"id": pid, "status": "error", "error": "The local AI isn't answering."}
                vecs = None
            if vecs is not None:
                for (pid, author, text, _), U in zip(todo, vecs):
                    out[pid] = self._fast(pid, author, text, U, now)
        with self.lock:   # copies: the judge thread may update a stored result while it is being sent
            return [copy.deepcopy(out.get(str(it.get("id", "")))) for it in items]

    def _fast(self, pid: str, author: str, text: str, U: np.ndarray, now: float) -> dict:
        t = self.t
        U = U if U.ndim == 2 else U[None]
        v = U[0]                       # whole post: source match + coordination (copy_strong is calibrated on it)
        src = self._top(self.bank.tg_emb, self.bank.tg, v, 3)
        strong = bool(src) and src[0][0] >= t["copy_strong"]
        source_match = {"strong": strong, "telegram": [
            {"channel": r.get("channel"), "url": r.get("url"), "date": r.get("date"), "sim": round(s, 4),
             "text": (r.get("text") or "")[:280]} for s, r in src]} if src else None
        cands = self._union(self.bank.claim_emb, self.bank.claims, U, 5)
        echo = [{"text": r["text"], "source": r.get("source"), "n_aired": r.get("n_mentions"),
                 "first": r.get("date_first"), "last": r.get("date_last"), "sim": round(s, 4)}
                for s, r in self._union(self.bank.echo_emb, self.bank.echo, U, 3)
                if s >= t["echo_min"]]
        reasons, others = [], set()
        if author:
            others = self.recent.add_and_match(pid, author, v, t["copy_strong"], now)
            with self.lock:
                self.author_seen[author] += 1
                if strong:
                    self.author_hits[author] += 1
            n_rep, n_seen = self.author_hits.get(author, 0), self.author_seen.get(author, 0)
            if len(others) >= t["copy_accounts"]:
                reasons.append(f"Same text posted by {len(others)} other accounts in the last hour.")
            if n_rep >= t["author_repeat"] and n_rep / n_seen >= t["author_share"]:
                reasons.append(f"{n_rep} of this account's {n_seen} posts we have seen copy pro-Kremlin Telegram posts.")
        coordination = {"label": "coordinated" if reasons else "none", "reasons": reasons,
                        "n_similar_accounts": len(others)}
        best = cands[0] if cands else None
        content = _none_content()
        content["narrative"] = {"text": best[1]["text"], "source": best[1].get("source"), "sim": round(best[0], 4)} if best else None
        content["state_media_echo"] = echo
        res = {"id": pid, "status": "done", "content": content, "source_match": source_match, "coordination": coordination}
        go = bool(self.pool and cands and (best[0] >= t["candidate_gate"] or strong))
        if go:
            res["status"] = "pending"
        self._store(pid, res)                 # store before the judge starts, so its update can't be lost
        if go:
            self.pool.submit(self._slow, pid, text, cands)
        return res

    # ---------------------------------------------------------------- slow path
    def _slow(self, pid: str, text: str, cands: list) -> None:
        try:
            j = self.judge(text, [r["text"] for _, r in cands])
            n = j.get("matched")
            hit = cands[n - 1] if j.get("ppg") and isinstance(n, int) and 1 <= n <= len(cands) else None
            upd = {"label": "potentially_propaganda" if hit else "none", "score": 1.0 if hit else 0.0,
                   "reason": j.get("reason", ""),
                   "techniques": [{"code": c, "name": TECHNIQUES.get(c, c), "quote": q} for c, q in j.get("techniques", [])]}
            if hit:
                upd["narrative"] = {"text": hit[1]["text"], "source": hit[1].get("source"), "sim": round(hit[0], 4)}
            with self.lock:
                r = self.results.get(pid)
                if r:
                    r["content"].update(upd)
                    r["status"] = "done"
        except Exception as exc:
            log.warning("judge failed for %s: %s", pid, exc)
            with self.lock:
                r = self.results.get(pid)
                if r:
                    r["status"] = "done"      # keep the fast-path fields; the content label stays "none"
                    r["content"]["reason"] = "The narrative check is unavailable right now."

    def _store(self, pid: str, res: dict) -> dict:
        with self.lock:
            self.results[pid] = res
            self.results.move_to_end(pid)
            while len(self.results) > 50000:
                self.results.popitem(last=False)
        return res

    def get(self, ids: list[str]) -> list[dict]:
        with self.lock:
            return [copy.deepcopy(self.results.get(i)) or {"id": i, "status": "error", "error": "unknown id"} for i in ids]

    def status(self) -> dict:
        """What the dashboard's System page shows: bank sizes, thresholds, judge on/off, embedding model."""
        b = self.bank
        return {"available": True, "reason": None,
                "claims": len(b.claims), "echoClaims": len(b.echo), "telegramPosts": len(b.tg),
                "thresholds": {k: self.t[k] for k in ("copy_strong", "candidate_gate", "echo_min") if k in self.t},
                "judge": self.judge is not None,
                "embeddingModel": b.manifest.get("embedding_model"), "dim": b.manifest.get("dim")}

    def feedback(self, row: dict) -> None:
        if not self.feedback_path:
            return
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self.feedback_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**row, "t": self.clock()}, ensure_ascii=False) + "\n")


def _none_content() -> dict:
    return {"label": "none", "score": 0.0, "narrative": None, "techniques": [], "reason": "", "state_media_echo": []}


def modal_judge(base_url: str, timeout: float = 60.0) -> Callable[[str, list[str]], dict]:
    """The fine-tuned Gemma judge behind the Modal playground: POST /api/analyze with our candidate claims."""
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def judge(text: str, claims: list[str]) -> dict:
        r = client.post("/api/analyze", json={"text": text, "claims": claims})
        r.raise_for_status()
        d = r.json()
        return {"ppg": d.get("ppg"), "matched": d.get("matched"), "reason": d.get("reason", ""),
                "techniques": d.get("techniques") or []}
    return judge


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine(ai_factory: Callable) -> Engine:
    """The process-wide engine, built on first use (the bank takes a few seconds and a few hundred MB to load)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            bank = Bank.load(Path(os.environ.get("NARRATIVE_BANK_DIR") or BANK_DIR))
            url = os.environ.get("NARRATIVE_JUDGE_URL") or JUDGE_URL
            url = "" if url == "off" else url
            ai = ai_factory()
            _engine = Engine(bank, embed=lambda texts: ai.embed_documents(list(texts)),
                             judge=modal_judge(url) if url else None,
                             workers=int(os.environ.get("NARRATIVE_JUDGE_WORKERS", "4")),
                             feedback_path=Path(os.environ.get("NARRATIVE_FEEDBACK") or ROOT / "data" / "narrative_feedback.jsonl"))
        return _engine


def set_engine(engine: Engine | None) -> None:
    """Tests swap the engine."""
    global _engine
    _engine = engine
