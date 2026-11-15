"""Collecting a post that isn't in the database yet: one interface, two providers.

  Oxylabs      Facebook and Telegram (Web Scraper API, realtime, "universal" source:
               POST https://realtime.oxylabs.io/v1/queries, Basic auth, the page's HTML
               comes back in results[0].content)
  context.dev  news sites and evidence pages (GET https://api.context.dev/v1/web/scrape/html?url=...,
               Bearer key). The endpoint shape is taken from public listings and still
               needs confirming against a real key; the parsing is ours, so only fetch_html changes.

Keys go in .env: OXYLABS_USERNAME, OXYLABS_PASSWORD, CONTEXT_DEV_API_KEY. Without
them nothing is fetched, and only posts already in the database can be analyzed.

Parsing is done here, from the HTML:
  Telegram  the public post widget (t.me/<channel>/<n>?embed=1): text, author, time, views
  Facebook  the page's Open Graph tags (og:title, og:description, article times): best effort;
            the team's own parser plugs in as another Collector.
  Articles  Open Graph / article meta tags plus the text of <article> (or all <p>).
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Protocol

import httpx

from .links import ParsedLink


class CollectError(Exception):
    """The post couldn't be collected. The message is shown to users as is.
    status: what the API answers with (404 not found/private, 429 rate limited, 502 bad answer, 503 no scraper)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CollectedPost:
    platform: str                       # "telegram", "facebook", "web"
    external_id: str
    url: str
    text: str
    account: str                        # channel/page name or site domain
    account_name: str                   # what's shown, e.g. "Telegram News"
    account_type: str = "unknown"       # channel, group, page, website, user
    published_at: datetime | None = None
    views: int | None = None
    title: str | None = None
    byline: str | None = None
    media: list[str] = field(default_factory=list)
    language: str | None = None         # BCP 47 code when the source says so (e.g. "lt"); None = let Gemma find out
    followers: int | None = None        # the account's subscriber count at collection time, when known


class Collector(Protocol):
    def available(self) -> bool: ...
    def collect(self, link: ParsedLink) -> CollectedPost: ...


# --------------------------------------------------------------------------- HTML parsing

# Tags that never have a closing tag.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Collect(HTMLParser):
    """Collects meta tags, and the text inside elements whose class (or tag) matches."""

    def __init__(self, text_class: str | None = None, text_tag: str | None = None):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.times: list[str] = []
        self.texts: list[str] = []
        self.classes: dict[str, str] = {}   # first text of interesting classes
        self.attrs_of: dict[str, dict] = {}
        self._text_class, self._text_tag = text_class, text_tag
        self._depth = 0          # >0 while inside the text element
        self._buf: list[str] = []
        self._watch: list[tuple[str, int]] = []
        self._level = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self._level += 0 if tag in _VOID else 1
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and "content" in a:
                self.meta.setdefault(key.lower(), a["content"])
        if tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag == "br" and self._depth:
            self._buf.append("\n")
        classes = a.get("class", "").split()
        for c in classes:
            self.attrs_of.setdefault(c, a)
        if self._depth:
            if tag not in _VOID:
                self._depth += 1
            return
        if (self._text_class and self._text_class in classes) or (self._text_tag and tag == self._text_tag):
            self._depth = 1
            self._buf = []
        for c in ("tgme_widget_message_owner_name", "tgme_widget_message_views", "tgme_widget_message_date"):
            if c in classes:
                self._watch.append((c, self._level))

    def handle_startendtag(self, tag, attrs):
        # <br/>: a start only (the default would also run handle_endtag and close the text early)
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return  # stray </br> and the like
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.texts.append("".join(self._buf))
            elif tag in ("p", "div"):
                self._buf.append("\n")
        self._watch = [(c, lvl) for c, lvl in self._watch if lvl < self._level]
        self._level = max(0, self._level - 1)

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)
        for c, _lvl in self._watch:
            if data.strip():
                self.classes.setdefault(c, data.strip())


def _clean(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in unescape(text).split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _count(value: str | None) -> int | None:
    """'2.92M' -> 2920000, '18.4K' -> 18400, '912' -> 912."""
    if not value:
        return None
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KkMm]?)", value.strip())
    if not m:
        return None
    return int(round(float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]))


def parse_telegram_widget(html: str, link: ParsedLink) -> CollectedPost:
    p = _Collect(text_class="tgme_widget_message_text")
    p.feed(html)
    if "tgme_widget_message_error" in p.attrs_of or "tgme_widget_message" not in " ".join(p.attrs_of):
        raise CollectError("Telegram says this post doesn't exist or isn't public.", 404)
    text = _clean(p.texts[0]) if p.texts else ""
    post_attrs = next((a for a in p.attrs_of.values() if a.get("data-post")), {})
    data_post = post_attrs.get("data-post", "")
    if data_post and link.external_id and data_post.lower() != link.external_id.lower() and not link.external_id.startswith("-100"):
        raise CollectError("Telegram returned a different post than the one asked for.", 502)
    media = re.findall(r"background-image:url\('([^']+)'\)", html)
    return CollectedPost(
        platform="telegram", external_id=link.external_id or data_post, url=link.url, text=text,
        account=link.account or data_post.split("/")[0], account_name=p.classes.get("tgme_widget_message_owner_name") or (link.account or ""),
        account_type="channel", published_at=_time(p.times[-1] if p.times else None),
        views=_count(p.classes.get("tgme_widget_message_views")), media=media[:10],
    )


def telegram_channel_id(html: str) -> int | None:
    """The channel's internal id, from the widget's data-view token (e.g. -1005640892)."""
    m = re.search(r'data-view="([A-Za-z0-9_\-]+)"', html)
    if not m:
        return None
    token = m.group(1)
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return int(data["c"])
    except (ValueError, KeyError, TypeError):
        return None


def parse_facebook_page(html: str, link: ParsedLink) -> CollectedPost:
    p = _Collect()
    p.feed(html)
    text = _clean(p.meta.get("og:description", ""))
    if not text:
        if "login" in html.lower() and "password" in html.lower():
            raise CollectError("Facebook only shows this post to logged-in users, so it can't be collected.", 404)
        raise CollectError("The Facebook page had no post text in it.", 502)
    name = _clean(p.meta.get("og:title", "")) or (link.account or "")
    published = _time(p.meta.get("article:published_time") or (p.times[0] if p.times else None))
    return CollectedPost(
        platform="facebook", external_id=link.external_id or "", url=link.url, text=text,
        account=link.account or "", account_name=name, account_type="page",
        published_at=published, media=[p.meta["og:image"]] if p.meta.get("og:image") else [],
    )


def parse_article(html: str, url: str) -> CollectedPost:
    article = _Collect(text_tag="article")
    article.feed(html)
    text = _clean("\n".join(article.texts))
    if len(text) < 200:
        paragraphs = _Collect(text_tag="p")
        paragraphs.feed(html)
        text = _clean("\n\n".join(t for t in paragraphs.texts if t.strip()))
    if not text:
        raise CollectError("The page had no article text in it.", 502)
    meta = article.meta
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    return CollectedPost(
        platform="web", external_id="", url=url, text=text, account=domain,
        account_name=_clean(meta.get("og:site_name", "")) or domain, account_type="website",
        published_at=_time(meta.get("article:published_time") or (article.times[0] if article.times else None)),
        title=_clean(meta.get("og:title", "")) or None,
        byline=_clean(meta.get("author", "") or meta.get("article:author", "")) or None,
        media=[meta["og:image"]] if meta.get("og:image") else [],
    )


# --------------------------------------------------------------------------- providers

def _check(res: httpx.Response, service: str) -> None:
    if res.status_code in (401, 403):
        raise CollectError(f"{service} refused the login. Check its keys in .env.", 503)
    if res.status_code == 429:
        raise CollectError(f"{service} says too many requests were made. Try again in a few minutes.", 429)
    if res.status_code >= 400:
        raise CollectError(f"{service} couldn't fetch the page (error {res.status_code}).", 502)


class Oxylabs:
    """Facebook and Telegram pages through Oxylabs' Web Scraper API."""
    URL = "https://realtime.oxylabs.io/v1/queries"

    def __init__(self, username: str, password: str, *, timeout: float = 90.0, transport: httpx.BaseTransport | None = None):
        self._auth = (username, password)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def available(self) -> bool:
        return bool(self._auth[0] and self._auth[1])

    def fetch_html(self, url: str) -> str:
        try:
            res = self._http.post(self.URL, auth=self._auth, json={"source": "universal", "url": url})
        except httpx.TimeoutException:
            raise CollectError("Oxylabs took too long to fetch the post. Try again.", 504) from None
        except httpx.TransportError:
            raise CollectError("Oxylabs can't be reached. Check the internet connection.", 503) from None
        _check(res, "Oxylabs")
        try:
            result = res.json()["results"][0]
            content = result["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise CollectError("Oxylabs sent an answer in an unexpected shape.", 502) from None
        page_status = result.get("status_code")
        if page_status in (404, 410):
            raise CollectError("That post doesn't exist any more, or isn't public.", 404)
        if not isinstance(content, str) or not content.strip():
            raise CollectError("Oxylabs sent an empty page.", 502)
        return content

    def collect(self, link: ParsedLink) -> CollectedPost:
        if link.platform == "telegram":
            if link.external_id and link.external_id.startswith("-100"):
                raise CollectError("Posts in private Telegram channels can't be collected.", 404)
            return parse_telegram_widget(self.fetch_html(link.url + "?embed=1&mode=tme"), link)
        if link.platform == "facebook":
            return parse_facebook_page(self.fetch_html(link.url), link)
        raise CollectError("Oxylabs collects only Facebook and Telegram posts.", 400)


class ContextDev:
    """News sites and evidence pages through context.dev."""
    URL = "https://api.context.dev/v1/web/scrape/html"

    def __init__(self, api_key: str, *, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def available(self) -> bool:
        return bool(self._key)

    def fetch_html(self, url: str) -> str:
        try:
            res = self._http.get(self.URL, params={"url": url}, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TimeoutException:
            raise CollectError("context.dev took too long to fetch the page. Try again.", 504) from None
        except httpx.TransportError:
            raise CollectError("context.dev can't be reached. Check the internet connection.", 503) from None
        _check(res, "context.dev")
        if "json" in res.headers.get("content-type", ""):
            try:
                data = res.json()
            except ValueError:
                raise CollectError("context.dev sent an answer that couldn't be read.", 502) from None
            html = data.get("html") or data.get("content") or (data.get("data") or {}).get("html") if isinstance(data, dict) else None
        else:
            html = res.text
        if not isinstance(html, str) or not html.strip():
            raise CollectError("context.dev sent an empty page.", 502)
        return html

    def collect_article(self, url: str) -> CollectedPost:
        return parse_article(self.fetch_html(url), url)


class NoCollector:
    """Used when no scraper keys are set: nothing can be fetched."""

    def available(self) -> bool:
        return False

    def collect(self, link: ParsedLink) -> CollectedPost:
        raise CollectError(NOT_SET_UP, 404)


NOT_SET_UP = ("We haven't collected that post yet, and no scraper is set up to fetch it. "
              "Add OXYLABS_USERNAME and OXYLABS_PASSWORD to .env, or analyze a post that is already collected.")


def collector_for(settings) -> Collector:
    """Oxylabs when its keys are in .env, otherwise nothing is fetched."""
    if settings.oxylabs_username and settings.oxylabs_password:
        return Oxylabs(settings.oxylabs_username, settings.oxylabs_password)
    return NoCollector()
