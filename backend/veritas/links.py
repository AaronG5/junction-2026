"""Which links Veritas Lens accepts: the same rule as isSupportedLink() in
frontend/src/api/client.ts, tested against shared/cases/links.json.

Spelled out character by character on both sides (no str.strip(), \\s or \\S),
because Python and JavaScript disagree on what counts as whitespace and on
Unicode case folding:
  - trim exactly these spaces: space, tab, newline, CR, form feed, vertical tab,
    no-break space (U+00A0) and the invisible byte-order mark (U+FEFF);
  - match the domain case-insensitively, ASCII only;
  - the rest of the link must be printable ASCII (! to ~) up to the end.
(Turning a link into platform + post id comes in B6.)
"""
from __future__ import annotations

import re

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
