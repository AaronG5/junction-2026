"""Keeping the posts the browser extension sends, so the collection grows while people browse.

Every post that reaches /api/ext/batch (the automatic check) or /api/ext/save (the extension's
copy to Veritas Lens on the reader's own computer) is stored in MySQL and embedded into Qdrant,
the same way the importer stores the team's collected data (veritas/importer.py):
  - X          platform "x", external_id = the status id, account = the handle
  - Facebook   platform "facebook", external_id as links.parse_link gives it ("<group>_<post>",
               "<page>_<post>", "<owner>_<story>"), else Facebook's post id; group posts belong to
               the group (account), with the author as the byline
A post stored before with the same text is skipped; a changed text is updated and embedded again.

It runs in a background thread, so the extension's requests never wait for it. When MySQL, Qdrant
or the AI isn't running, the posts of that round are skipped (and logged); posts whose rows were
stored but not embedded are picked up by `import_data.py --embed` (embed_pending) later.
Turn it off with EXT_SAVE_POSTS=off.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable

from .collectors import CollectedPost
from .links import parse_link

log = logging.getLogger("veritas.ext_store")

MAX_QUEUE = 5000
ROUND = 200            # posts per round
WAIT_S = 2.0           # seconds the worker waits to collect a round
_X_STATUS = re.compile(r"^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{5,25})")
_LANG = re.compile(r"^[a-z]{2}$")


def _time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def to_collected(item: dict) -> CollectedPost | None:
    """One extension item (extension/API.md) -> CollectedPost; None when it can't be stored
    (no text, no link, or a synthetic id for a quoted post without a permalink)."""
    text = (item.get("text") or "").strip()
    url = (item.get("url") or "").strip()
    if not text or not url.startswith("https://"):
        return None
    lang = (item.get("lang") or "").lower()
    language = lang if _LANG.match(lang) else None
    published = _time(item.get("time"))
    author_name = (item.get("author_name") or "").strip()[:255] or None

    m = _X_STATUS.match(url)
    if m:
        handle = m.group(1).lower()
        return CollectedPost(platform="x", external_id=m.group(2), url=f"https://x.com/{m.group(1)}/status/{m.group(2)}",
                             text=text, account=handle, account_name=author_name or handle, account_type="user",
                             published_at=published, language=language)

    if item.get("platform") == "facebook" or "facebook.com/" in url:
        link = parse_link(url)
        group = (item.get("group") or "").strip()[:255] or None
        author = (item.get("author") or "").strip().lower()[:255] or None
        if link is not None and link.kind == "post" and link.external_id:
            external_id, clean_url, owner = link.external_id, link.url, link.account
        else:  # reels, videos, photos: Facebook's own post id
            pid = re.sub(r"^fb", "", str(item.get("id") or ""))
            if not pid:
                return None
            external_id, clean_url, owner = pid, url.split("?")[0], None
        if "/groups/" in clean_url and owner:
            return CollectedPost(platform="facebook", external_id=external_id, url=clean_url, text=text,
                                 account=owner, account_name=group or owner, account_type="group",
                                 published_at=published, byline=author_name, language=language)
        account = author or owner
        if not account:
            return None
        return CollectedPost(platform="facebook", external_id=external_id, url=clean_url, text=text,
                             account=account, account_name=author_name or account, account_type="unknown",
                             published_at=published, language=language)
    return None


def save_posts(conn, posts: Iterable[CollectedPost]) -> list[tuple[int, str]]:
    """Store the posts; returns (post_id, text) for the new and changed ones (they need a vector)."""
    from .analysis import save_collected
    from .ids import post_uuid

    changed = []
    for c in posts:
        with conn.cursor() as cur:
            cur.execute("SELECT post_id, text FROM posts WHERE uuid = %s", (post_uuid(c.platform, c.external_id).bytes,))
            existing = cur.fetchone()
        if existing is not None and existing["text"] == c.text:
            continue
        post_id = save_collected(conn, c)
        if existing is not None:
            with conn.cursor() as cur:   # the old vector no longer matches the text
                cur.execute("UPDATE posts SET embedded_at = NULL, embedding_model_id = NULL WHERE post_id = %s", (post_id,))
            conn.commit()
        changed.append((post_id, c.text))
    return changed


class PostStore:
    """Queue + one worker thread. add() never blocks and never raises."""

    def __init__(self, settings, ai_factory: Callable, qdrant_factory: Callable, *, start: bool = True):
        self.settings, self.ai_factory, self.qdrant_factory = settings, ai_factory, qdrant_factory
        self.queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self.stored = 0      # posts written (new or changed) since start
        self.embedded = 0
        self._thread = threading.Thread(target=self._work, name="ext-store", daemon=True) if start else None
        if self._thread:
            self._thread.start()

    def add(self, items: Iterable[dict]) -> int:
        n = 0
        for it in items:
            c = to_collected(it) if isinstance(it, dict) else None
            if c is None:
                continue
            try:
                self.queue.put_nowait(c)
                n += 1
            except queue.Full:
                log.warning("ext store: queue full, dropping posts")
                break
        return n

    def _take(self) -> list[CollectedPost]:
        out = []
        try:
            out.append(self.queue.get(timeout=WAIT_S))
            while len(out) < ROUND:
                out.append(self.queue.get_nowait())
        except queue.Empty:
            pass
        return out

    def run_once(self, posts: list[CollectedPost]) -> None:
        """Store one round (also called directly by tests)."""
        from .content import store_post_vector
        from .db import connection

        seen, unique = set(), []
        for c in posts:                      # the same post twice in one round: keep the last
            key = (c.platform, c.external_id)
            if key in seen:
                unique = [u for u in unique if (u.platform, u.external_id) != key]
            seen.add(key)
            unique.append(c)
        try:
            with connection(self.settings) as conn:
                changed = save_posts(conn, unique)
                self.stored += len(changed)
                if not changed:
                    return
                try:
                    ai = self.ai_factory()
                    try:
                        vectors = ai.embed_documents([t for _, t in changed])
                    finally:
                        ai.close()
                    with self.qdrant_factory() as q:
                        for (post_id, _), v in zip(changed, vectors):
                            store_post_vector(conn, q, post_id, v)
                    self.embedded += len(changed)
                except Exception as exc:     # rows are kept; embed_pending() makes the vectors later
                    log.info("ext store: %d posts stored without vectors (%s)", len(changed), exc)
        except Exception as exc:
            log.warning("ext store: %d posts not stored (%s)", len(unique), exc)

    def _work(self) -> None:
        while True:
            posts = self._take()
            if posts:
                self.run_once(posts)


_store: PostStore | None = None
_store_lock = threading.Lock()


def get_store(settings) -> PostStore | None:
    """The process-wide store, or None when saving is turned off (EXT_SAVE_POSTS=off)."""
    global _store
    if (os.environ.get("EXT_SAVE_POSTS") or "on").lower() in ("off", "0", "false", "no"):
        return None
    with _store_lock:
        if _store is None:
            from .ai import LocalAI
            from .vectors import Qdrant
            _store = PostStore(settings, lambda: LocalAI.from_settings(settings), lambda: Qdrant.from_settings(settings))
        return _store


def set_store(store: PostStore | None) -> None:
    """Tests swap the store."""
    global _store
    _store = store
