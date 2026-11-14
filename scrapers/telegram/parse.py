"""Parse Telegram's public channel preview (t.me/s/<username>) into posts.

Each post becomes a dict with exactly these fields (docs/oxylabs-telegram-scrape.md):
    author       post signature if there is one, else the channel display name
    content      the post's own text, <br> as "\n"; "" for media-only posts
    dateTime     UTC, ISO 8601 with a Z suffix
    channelName  the `name` from data/positives/telegram_sources.json
    postUrl      https://t.me/<data-post>, e.g. https://t.me/infalt/1234

Selectors were checked against a real page (t.me/s/infalt, 2026-11-14).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

TME = "https://t.me/"

MESSAGE = "div.tgme_widget_message[data-post]"
SERVICE = "service_message"
REPLY_QUOTE = "tgme_widget_message_reply"     # quoted message inside a reply
SIGNATURE = ".tgme_widget_message_from_author"
OWNER_NAME = ".tgme_widget_message_owner_name"
TEXT = ".tgme_widget_message_text"
DATE = ".tgme_widget_message_date time[datetime]"
CHANNEL_TITLE = ".tgme_channel_info_header_title"


@dataclass
class ParsedPage:
    posts: list[dict] = field(default_factory=list)       # page order (oldest first)
    message_ids: list[int] = field(default_factory=list)  # every message, service ones too

    @property
    def has_messages(self) -> bool:
        return bool(self.message_ids)


def parse_page(html: str, channel_name: str) -> ParsedPage:
    soup = BeautifulSoup(html or "", "html.parser")
    title = _text(soup.select_one(CHANNEL_TITLE))
    page = ParsedPage()
    for msg in soup.select(MESSAGE):
        data_post = msg["data-post"].strip()
        mid = message_id(data_post)
        if mid is None:
            continue
        page.message_ids.append(mid)
        if SERVICE in (msg.get("class") or []):
            continue
        page.posts.append({
            "author": _author(msg) or title or channel_name,
            "content": _content(msg),
            "dateTime": _date_time(msg),
            "channelName": channel_name,
            "postUrl": TME + data_post,
        })
    return page


def message_id(data_post_or_url: str) -> int | None:
    """'infalt/1234' or 'https://t.me/infalt/1234' -> 1234."""
    tail = data_post_or_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _own(msg: Tag, selector: str) -> Tag | None:
    """First match that belongs to the post itself, not to a quoted reply."""
    for el in msg.select(selector):
        if not any(REPLY_QUOTE in (p.get("class") or [])
                   for p in el.parents if p is not msg and isinstance(p, Tag)):
            return el
    return None


def _text(el: Tag | None) -> str:
    return el.get_text(strip=True) if el is not None else ""


def _author(msg: Tag) -> str:
    return _text(_own(msg, SIGNATURE)) or _text(_own(msg, OWNER_NAME))


def _content(msg: Tag) -> str:
    block = _own(msg, TEXT)
    if block is None:
        return ""
    for br in block.find_all("br"):
        br.replace_with("\n")
    # get_text keeps link text (not hrefs) and emoji, and unescapes entities.
    return block.get_text().strip()


def _date_time(msg: Tag) -> str | None:
    el = _own(msg, DATE)
    if el is None:
        return None
    try:
        dt = datetime.fromisoformat(el["datetime"].strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_utc_iso(dt)


def to_utc_iso(dt: datetime) -> str:
    """The dateTime format: UTC, ISO 8601, Z suffix. Naive datetimes count as UTC.
    The fixed width means these strings sort and compare in time order."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
