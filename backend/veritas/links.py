"""Which links Veritas Lens accepts: the same rule as isSupportedLink() in
frontend/src/api/client.ts, tested against shared/cases/links.json.

Spelled out character by character on both sides (no str.strip(), \\s or \\S),
because Python and JavaScript disagree on what counts as whitespace and on
Unicode case folding:
  - trim exactly these spaces: space, tab, newline, CR, form feed, vertical tab,
    no-break space (U+00A0) and the invisible byte-order mark (U+FEFF);
  - match the domain case-insensitively, ASCII only;
  - the rest of the link must be printable ASCII (! to ~) up to the end.
parse_link() says what a link points at (platform, account, post id).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SPACE = "[ \t\n\r\f\v\u00a0\ufeff]"
_TRIM = re.compile(f"^{_SPACE}+|{_SPACE}+$")
_LINK = re.compile(r"(https?://)?(t\.me|telegram\.me|(www\.|m\.)?facebook\.com|fb\.com)/[!-~]+", re.IGNORECASE | re.ASCII)


def is_supported_link(value: str) -> bool:
    return _LINK.fullmatch(_TRIM.sub("", value)) is not None


_HOST_ALIASES = {"telegram.me": "t.me", "fb.com": "facebook.com"}
_SCHEME = re.compile(r"^https?://", re.IGNORECASE | re.ASCII)
_UPPER = re.compile("[A-Z]")


def link_key(value: str) -> str:
    """One spelling per link, for looking it up: spaces trimmed, no http(s)://, no
    www. or m., telegram.me = t.me, fb.com = facebook.com, no #part, no trailing
    slash, ASCII lowercase, and no ?query (tracking) except on .php pages such as
    Facebook's story.php?story_fbid=..., where the query says which post it is.
    "HTTPS://T.me/Kanal/5/?x=1" -> "t.me/kanal/5".
    Same as linkKey() in frontend/src/api/client.ts (shared cases: links.json "keys")."""
    s = _SCHEME.sub("", _TRIM.sub("", value))
    s = s.partition("#")[0]
    s, mark, query = s.partition("?")
    s = s.rstrip("/")
    if re.search(r"\.php\Z", s, re.IGNORECASE | re.ASCII) and query:  # \Z: $ would also match before a final newline
        s += mark + query
    s = _UPPER.sub(lambda m: m.group().lower(), s)
    host, slash, path = s.partition("/")
    host = re.sub(r"^(www\.|m\.)", "", host)
    return _HOST_ALIASES.get(host, host) + slash + path


# --------------------------------------------------------------------------- what a link points at

@dataclass(frozen=True)
class ParsedLink:
    """platform: "telegram" or "facebook". kind: "post", "account" or "other".
    account: the channel/page/group name (lowercase) or numeric id.
    external_id: the post's id as stored in posts.external_id (None when the link
    alone doesn't say, e.g. Facebook share links)."""
    platform: str
    kind: str
    account: str | None = None
    post: str | None = None
    external_id: str | None = None
    url: str = ""                      # a clean https:// link to the same thing


_TG_NAME = re.compile(r"^[a-z][a-z0-9_]{3,31}$")
_DIGITS = re.compile(r"^[0-9]{1,20}$")
_FB_RESERVED = {"groups", "watch", "photo", "photo.php", "permalink.php", "story.php", "share", "profile.php",
                "events", "marketplace", "reel", "reels", "stories", "hashtag", "login", "help", "pages", "people"}


def _query_param(query: str, name: str) -> str | None:
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name and value:
            return value
    return None


def parse_link(value: str) -> ParsedLink | None:
    """What a supported link points at; None for links Veritas Lens doesn't accept.

    Telegram  t.me/<name>/<n>, t.me/s/<name>/<n>, t.me/<name>/<topic>/<n>  -> post "<name>/<n>"
              t.me/c/<id>/<n> (private channel)                            -> post "-100<id>/<n>"
              t.me/<name>                                                  -> account
    Facebook  facebook.com/<page>/posts/<id>, /<page>/videos/<id>          -> post "<page>_<id>"
              permalink.php or story.php?story_fbid=<p>&id=<owner>         -> post "<owner>_<p>"
              /groups/<group>/posts/<id>, /groups/<group>/permalink/<id>   -> post "<group>_<id>"
              /<page>, /profile.php?id=<n>, /groups/<group>                -> account
              /share/..., /photo..., /reel/...                             -> other (not identifiable from the link)
    """
    if not is_supported_link(value):
        return None
    key = link_key(value)
    host, _, rest = key.partition("/")
    path, _, query = rest.partition("?")
    parts = [p for p in path.split("/") if p]
    if host == "t.me":
        if parts[:1] == ["s"]:
            parts = parts[1:]
        if len(parts) >= 3 and parts[0] == "c" and _DIGITS.match(parts[1]) and _DIGITS.match(parts[-1]):
            ext = f"-100{parts[1]}/{parts[-1]}"
            return ParsedLink("telegram", "post", f"-100{parts[1]}", parts[-1], ext, f"https://t.me/c/{parts[1]}/{parts[-1]}")
        if parts and _TG_NAME.match(parts[0]):
            name = parts[0]
            if len(parts) == 1:
                return ParsedLink("telegram", "account", name, url=f"https://t.me/{name}")
            if len(parts) in (2, 3) and all(_DIGITS.match(p) for p in parts[1:]):
                return ParsedLink("telegram", "post", name, parts[-1], f"{name}/{parts[-1]}", f"https://t.me/{name}/{parts[-1]}")
        return ParsedLink("telegram", "other", url=f"https://{key}")
    # facebook.com (link_key already folded fb.com, www. and m.)
    if parts and parts[0] in ("permalink.php", "story.php"):
        post, owner = _query_param(query, "story_fbid"), _query_param(query, "id")
        if post and owner:
            return ParsedLink("facebook", "post", owner, post, f"{owner}_{post}",
                              f"https://www.facebook.com/permalink.php?story_fbid={post}&id={owner}")
    if parts and parts[0] == "profile.php":
        owner = _query_param(query, "id")
        if owner and _DIGITS.match(owner):
            return ParsedLink("facebook", "account", owner, url=f"https://www.facebook.com/profile.php?id={owner}")
    if parts[:1] == ["groups"] and len(parts) >= 2:
        group = parts[1]
        if len(parts) == 2:
            return ParsedLink("facebook", "account", group, url=f"https://www.facebook.com/groups/{group}")
        if len(parts) == 4 and parts[2] in ("posts", "permalink"):
            return ParsedLink("facebook", "post", group, parts[3], f"{group}_{parts[3]}",
                              f"https://www.facebook.com/groups/{group}/posts/{parts[3]}")
    if parts and parts[0] not in _FB_RESERVED:
        page = parts[0]
        if len(parts) == 1:
            return ParsedLink("facebook", "account", page, url=f"https://www.facebook.com/{page}")
        if len(parts) == 3 and parts[1] in ("posts", "videos"):
            return ParsedLink("facebook", "post", page, parts[2], f"{page}_{parts[2]}",
                              f"https://www.facebook.com/{page}/{parts[1]}/{parts[2]}")
    return ParsedLink("facebook", "other", url=f"https://{key}")
