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
