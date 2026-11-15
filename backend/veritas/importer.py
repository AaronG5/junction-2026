"""Loading the team's collected data (data/parsed/) into MySQL and Qdrant.

The scrapers write JSON files; nothing else read them. This module turns each file
into the same CollectedPost records the "Analyze a link" collectors produce, stores
them through analysis.save_collected (so ids, accounts and copies behave exactly like
posts collected live), and then embeds every post that has no vector yet.

  Telegram    data/parsed/telegram/posts.json      {"<username>": [{author, content, dateTime, channelName, postUrl}]}
              plus data/positives/telegram_sources.json for each channel's subscriber count
  Articles    data/parsed/<site>/articles.json     {"site": "respublika.lt", "articles": [{url, headline, text, ...}]}

Rules (docs/data-architecture.md, docs/oxylabs-telegram-scrape.md):
  - Telegram external_id = the path of postUrl, lowercased ("infalt/4521"); the account is the username, lowercased.
  - Web external_id = sha256 of the canonical URL (hex); the account is the site's domain.
  - A post is written once: the same file loaded twice changes nothing, and a post whose
    text changed is updated and embedded again. Posts without text are left out.

Runs in two passes so a long embedding job can be stopped and continued:
  load_posts()     rows into MySQL (fast, no AI needed)
  embed_pending()  vectors into Qdrant for every post with embedded_at NULL, in batches,
                   each batch committed before the next; a second run continues where it stopped
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import urlsplit

from .ai import LocalAI, document_input
from .analysis import save_collected
from .collectors import CollectedPost
from .content import store_post_vector
from .ids import post_uuid
from .vectors import Qdrant

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
TELEGRAM_POSTS = DATA_DIR / "parsed" / "telegram" / "posts.json"
TELEGRAM_SOURCES = DATA_DIR / "positives" / "telegram_sources.json"
# Name on the command line -> file. Article files are found by their folder.
SOURCES: dict[str, Path] = {
    "telegram": TELEGRAM_POSTS,
    "respublika": DATA_DIR / "parsed" / "respublika" / "articles.json",
    "bukime_vieningi": DATA_DIR / "parsed" / "bukime_vieningi" / "articles.json",
}
# Sites whose language we know for sure; other sites are left for Gemma to find out.
SITE_LANGUAGE = {"respublika.lt": "lt", "bukimevieningi.lt": "lt"}
EMBED_BATCH = 32

Progress = Callable[[str], None]


class DataFileError(Exception):
    """A data file can't be used. The message is shown as is."""


# --------------------------------------------------------------------------- the files -> CollectedPost

def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise DataFileError(f"There is no data file at {path}.") from None
    except (OSError, ValueError) as exc:
        raise DataFileError(f"The data file {path} can't be read: {exc}") from None


def telegram_username(link: str) -> str:
    """'https://t.me/InfodefenseLIT' -> 'infodefenselit'."""
    return urlsplit(link.strip()).path.strip("/").split("/")[0].lower()


def telegram_subscribers(path: Path | None = None) -> dict[str, int]:
    """username (lowercase) -> subscriber count from the channel list; {} when the file is missing."""
    path = path if path is not None else TELEGRAM_SOURCES
    if not path.is_file():
        return {}
    out: dict[str, int] = {}
    for entry in _read_json(path).get("TG", []):
        name = telegram_username(entry.get("link", ""))
        size = entry.get("userSize")
        if name and isinstance(size, int) and size >= 0:
            out[name] = size
    return out


def telegram_post(raw: dict, subscribers: dict[str, int] | None = None) -> CollectedPost | None:
    """One posts.json entry -> CollectedPost; None when it has no text or no usable link."""
    text = (raw.get("content") or "").strip()
    url = (raw.get("postUrl") or "").strip()
    external_id = urlsplit(url).path.strip("/").lower()
    if not text or external_id.count("/") != 1:
        return None
    account = external_id.split("/")[0]
    channel_name = (raw.get("channelName") or "").strip() or account
    author = (raw.get("author") or "").strip()
    return CollectedPost(
        platform="telegram", external_id=external_id, url=url, text=text,
        account=account, account_name=channel_name, account_type="channel",
        published_at=_utc(raw.get("dateTime")),
        byline=author if author and author != channel_name else None,  # a signature, not the channel's own name
        followers=(subscribers or {}).get(account),
    )


def telegram_posts(path: Path | None = None, sources: Path | None = None) -> Iterator[CollectedPost]:
    """Every post with text in posts.json, in the file's order. sources: the channel list (subscriber counts)."""
    path = path if path is not None else TELEGRAM_POSTS
    data = _read_json(path)
    if not isinstance(data, dict):
        raise DataFileError(f"{path} should hold one list of posts per channel.")
    subscribers = telegram_subscribers(sources)
    for channel_posts in data.values():
        for raw in channel_posts or []:
            post = telegram_post(raw, subscribers)
            if post is not None:
                yield post


def canonical_url(url: str) -> str:
    """One spelling per article: https, lowercase host, no query, no #part, no trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return f"https://{host}{path}"


def web_external_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def article_post(raw: dict, site: str) -> CollectedPost | None:
    """One articles.json entry -> CollectedPost; None when it has no text or no link."""
    text = (raw.get("text") or "").strip()
    url = (raw.get("url") or "").strip()
    if not text or not urlsplit(url).netloc:
        return None
    headline = (raw.get("headline") or "").strip() or None
    author = (raw.get("author") or "").strip() or None
    return CollectedPost(
        platform="web", external_id=web_external_id(url), url=url, text=text,
        account=site, account_name=site, account_type="website",
        published_at=_utc(raw.get("published")), title=headline, byline=author,
        language=SITE_LANGUAGE.get(site),
    )


def article_posts(path: Path) -> Iterator[CollectedPost]:
    data = _read_json(path)
    site = (data.get("site") or "").strip().lower() if isinstance(data, dict) else ""
    if not site or not isinstance(data.get("articles"), list):
        raise DataFileError(f"{path} should hold a 'site' and a list of 'articles'.")
    for raw in data["articles"]:
        post = article_post(raw, site)
        if post is not None:
            yield post


def records(name: str, sources: dict[str, Path] | None = None) -> Iterator[CollectedPost]:
    """The posts in one named source (see SOURCES)."""
    files = sources if sources is not None else SOURCES
    if name not in files:
        raise DataFileError(f"Unknown source {name!r}. Choose from: {', '.join(files)}.")
    if name == "telegram":
        return telegram_posts(files[name])
    return article_posts(files[name])


# --------------------------------------------------------------------------- pass 1: rows

@dataclass
class Counts:
    new: int = 0            # posts stored for the first time
    updated: int = 0        # posts whose text changed (embedded again)
    unchanged: int = 0      # already stored with the same text
    accounts: set = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.new + self.updated + self.unchanged

    def summary(self) -> str:
        return (f"{self.total} posts from {len(self.accounts)} accounts: {self.new} new, {self.updated} updated,"
                f" {self.unchanged} already there")


def load_posts(conn, posts: Iterable[CollectedPost], *, progress: Progress | None = None,
               every: int = 1000) -> Counts:
    """Store the posts (accounts and metrics too). Posts already stored with the same text are skipped;
    a changed text is updated and marked for embedding again."""
    counts = Counts()
    for c in posts:
        counts.accounts.add((c.platform, c.account))
        with conn.cursor() as cur:
            cur.execute("SELECT post_id, text FROM posts WHERE uuid = %s", (post_uuid(c.platform, c.external_id).bytes,))
            existing = cur.fetchone()
        if existing is not None and existing["text"] == c.text:
            counts.unchanged += 1  # if its vector is still missing, embed_pending() makes it
        else:
            post_id = save_collected(conn, c)
            if existing is None:
                counts.new += 1
            else:
                counts.updated += 1
                with conn.cursor() as cur:  # the old vector no longer matches the text
                    cur.execute("UPDATE posts SET embedded_at = NULL, embedding_model_id = NULL WHERE post_id = %s", (post_id,))
                conn.commit()
        if progress and counts.total % every == 0:
            progress(f"  {counts.total} posts...")
    return counts


# --------------------------------------------------------------------------- pass 2: vectors

def count_pending(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL AND is_stub = FALSE AND deleted_at IS NULL"
                    " AND text IS NOT NULL AND text <> ''")
        return int(cur.fetchone()["n"])


# Vectors computed elsewhere (e.g. on a cloud GPU) with the same model and the same document_input(), keyed by
# vector_key(). With them, a fresh server stores tens of thousands of vectors in minutes instead of hours.
VECTORS = ROOT / "data" / "vectors" / "posts_768.npz"


def vector_key(text: str, title: str | None = None) -> str:
    return hashlib.sha256(document_input(text, title).encode("utf-8")).hexdigest()[:32]


def load_precomputed(path: Path | None = None) -> dict:
    """{vector_key: vector} from an .npz with arrays "keys" (str) and "vectors" (float16/32, n x 768). {} if absent."""
    path = path if path is not None else VECTORS
    if not path.is_file():
        return {}
    import numpy as np  # only needed for this path
    with np.load(path, allow_pickle=False) as z:
        keys, vecs = z["keys"], z["vectors"]
        if len(keys) != len(vecs):
            raise DataFileError(f"{path}: {len(keys)} keys but {len(vecs)} vectors.")
        return {str(k): vecs[i] for i, k in enumerate(keys)}


def embed_pending(conn, qdrant: Qdrant, ai: LocalAI | None, *, batch: int = EMBED_BATCH, limit: int | None = None,
                  progress: Progress | None = None, precomputed: dict | None = None) -> int:
    """Give every post without a vector one, oldest id first. Returns how many were embedded.
    Each batch is stored and committed before the next, so a stopped run loses at most one batch.
    precomputed: {vector_key: vector}; posts found there skip the AI. Without an AI, posts not found stay pending."""
    done = 0
    last_id = 0
    precomputed = precomputed or {}
    while limit is None or done < limit:
        size = batch if limit is None else min(batch, limit - done)
        with conn.cursor() as cur:
            cur.execute("SELECT post_id, title, text FROM posts WHERE embedded_at IS NULL AND is_stub = FALSE"
                        " AND deleted_at IS NULL AND text IS NOT NULL AND text <> '' AND post_id > %s"
                        " ORDER BY post_id LIMIT %s", (last_id, size))
            rows = cur.fetchall()
        if not rows:
            break
        found = {r["post_id"]: precomputed.get(vector_key(r["text"], r["title"])) for r in rows}
        missing = [r for r in rows if found[r["post_id"]] is None]
        if missing and ai is not None:
            for r, v in zip(missing, ai.embed_documents([r["text"] for r in missing], [r["title"] for r in missing])):
                found[r["post_id"]] = v
        for row in rows:
            vector = found[row["post_id"]]
            if vector is not None:
                store_post_vector(conn, qdrant, row["post_id"], [float(x) for x in vector])
                done += 1
        last_id = rows[-1]["post_id"]
        if progress:
            progress(f"  {done} vectors stored...")
    return done


# --------------------------------------------------------------------------- the whole run

def run_import(conn, names: Iterable[str], *, qdrant: Qdrant | None = None, ai: LocalAI | None = None,
               embed: bool = True, batch: int = EMBED_BATCH, limit: int | None = None,
               progress: Progress | None = None, sources: dict[str, Path] | None = None,
               precomputed: dict | None = None) -> dict:
    """Load the named sources, then embed what has no vector. Returns the counts per source and the
    number of vectors made. With embed=False (or no AI/Qdrant), only the rows are loaded."""
    say = progress or (lambda message: None)
    result: dict = {"sources": {}, "embedded": 0, "pending": 0}
    for name in names:
        say(f"Loading {name}...")
        posts = records(name, sources)
        if limit is not None:
            posts = _take(posts, limit)
        counts = load_posts(conn, posts, progress=progress)
        result["sources"][name] = counts
        say(f"  {counts.summary()}")
    if embed and qdrant is not None and (ai is not None or precomputed):
        pending = count_pending(conn)
        if pending:
            if precomputed:
                say(f"Storing vectors for {pending} posts ({len(precomputed)} precomputed; the AI makes only the rest)...")
            else:
                say(f"Making vectors for {pending} posts (this can take a while; stop it any time and run again to continue)...")
            result["embedded"] = embed_pending(conn, qdrant, ai, batch=batch, limit=limit, progress=progress,
                                               precomputed=precomputed)
    result["pending"] = count_pending(conn)
    return result


def _take(items: Iterable[CollectedPost], n: int) -> Iterator[CollectedPost]:
    for i, item in enumerate(items):
        if i >= n:
            return
        yield item
