"""Claims and fact checks with Gemini (veritas/gemini.py).

Claims (prompt claims_strict.v1): Gemini lists the claims in a post that are worth checking,
each written to stand on its own (names instead of "he", dates instead of "next year", using
the post's publication date), in neutral English, with the post's own words as the quote.
Trivial ones ("low") are dropped, and a quote that isn't in the post is cleared.

Fact checks (prompt factcheck.v1): Gemini searches Google and gives a verdict (true / false /
misleading / unverified), a confidence and a plain explanation naming its sources. The sources
stored as evidence are the pages the search actually used (Google's grounding data, resolved to
the real page), never links the model wrote. Guards:
  - a verdict other than "unverified" needs at least one source and a confidence of 0.5 or more,
    otherwise it becomes "unverified";
  - a claim is checked once; it is checked again only when asked (recheck).

Gemini is never used when Veritas Lens is set to work only on this computer (data mode
"local"), or without GEMINI_API_KEY: then claims come from the local AI and stay unchecked.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Callable, Iterable, Literal

from pydantic import Field

from . import app_settings
from .ai import Answer
from .analyzers import ClaimsAnswer, ExtractedClaim, _Strict, find_wording
from .config import Settings
from .gemini import Gemini, GeminiAnswer
from .vectors import Qdrant

log = logging.getLogger("veritas.factcheck")

CLAIMS_PROMPT = "claims_strict"
CHECK_PROMPT = "factcheck"
KEEP = ("high", "medium")
MAX_CLAIMS = 5
MIN_CONFIDENCE = 0.5
MAX_EVIDENCE = 6
STANCE = {"true": "supports", "false": "refutes", "misleading": "context", "unverified": "context"}
NO_SOURCES = "No reliable source about this was found, so it stays unverified."
UNSURE = "The sources found weren't conclusive, so it stays unverified."


# --------------------------------------------------------------------------- answer schemas (sent to Gemini)

class StrictClaim(_Strict):
    claim: Annotated[str, Field(max_length=400)]
    quote: Annotated[str, Field(max_length=400)]
    checkworthiness: Literal["high", "medium", "low"]
    topic: Annotated[str, Field(max_length=60)]


class StrictClaims(_Strict):
    claims: list[StrictClaim] = Field(max_length=6)


class FactCheckAnswer(_Strict):
    verdict: Literal["true", "false", "misleading", "unverified"]
    confidence: Annotated[float, Field(ge=0, le=1)]
    explanation: Annotated[str, Field(max_length=1500)]


# --------------------------------------------------------------------------- setup

def gemini_for(conn, settings: Settings) -> Gemini | None:
    """Gemini, when it is set up and the data mode allows requests to leave this computer."""
    if not settings.gemini_api_key:
        return None
    if app_settings.load(conn).data_mode == "local":
        return None
    return Gemini(settings.gemini_api_key, settings.gemini_model)


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


# --------------------------------------------------------------------------- claims

def extract_claims(gemini: Gemini, text: str, *, language: str, published: str, source: str) -> Answer[ClaimsAnswer]:
    """The post's claims worth checking, in the shape the rest of the app stores (analysis._store_claims)."""
    from .ai import load_prompt
    answer: GeminiAnswer[StrictClaims] = gemini.ask(load_prompt(CLAIMS_PROMPT), StrictClaims, text=text, language=language,
                                                    published=published or "unknown", source=source or "unknown")
    kept, seen = [], set()
    for c in answer.value.claims:
        claim = " ".join(c.claim.split())
        if c.checkworthiness not in KEEP or not claim or claim.lower() in seen:
            continue
        seen.add(claim.lower())
        kept.append(ExtractedClaim(claim=claim, quote=find_wording(text, c.quote) or ""))
    # the most important first; the stored shape (ClaimsAnswer) holds at most MAX_CLAIMS
    return Answer(ClaimsAnswer(claims=kept[:MAX_CLAIMS]), answer.model, answer.prompt)


# --------------------------------------------------------------------------- fact checks

def check_claim(conn, gemini: Gemini, qdrant: Qdrant | None, claim_id: int, today: str | None = None) -> str | None:
    """Fact-check one claim and store the verdict and its evidence. Returns the verdict
    (None when the claim doesn't exist)."""
    from .ai import load_prompt
    rows = _rows(conn, "SELECT claim_id, uuid, text, first_seen FROM claims WHERE claim_id = %s", (claim_id,))
    if not rows:
        return None
    claim = rows[0]
    today = today or datetime.now(timezone.utc).date().isoformat()
    answer = gemini.ask(load_prompt(CHECK_PROMPT), FactCheckAnswer, search=True, claim=claim["text"],
                        first_seen=claim["first_seen"].isoformat() if claim["first_seen"] else "unknown", today=today)
    verdict, confidence = answer.value.verdict, round(answer.value.confidence, 4)
    explanation = answer.value.explanation.strip()
    sources = list(answer.sources)[:MAX_EVIDENCE]
    if verdict != "unverified" and not sources:
        verdict, explanation = "unverified", f"{NO_SOURCES} {explanation}".strip()
    elif verdict != "unverified" and confidence < MIN_CONFIDENCE:
        verdict, explanation = "unverified", f"{UNSURE} {explanation}".strip()

    method = f"{answer.model} + Google Search"[:100]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM claim_evidence WHERE claim_id = %s AND method LIKE 'gemini%%'", (claim_id,))
        for s in sources:
            url = gemini.resolve(s.url)
            cur.execute("INSERT INTO claim_evidence (claim_id, url, title, publisher, stance, excerpt, method)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (claim_id, url[:2048], s.title[:1024], s.title[:255], STANCE[verdict],
                         " ".join(s.backs)[:2000] or None, "gemini+google_search"))
        cur.execute("UPDATE claims SET verdict = %s, verdict_confidence = %s, verdict_explanation = %s, verdict_method = %s,"
                    " verdict_at = %s WHERE claim_id = %s",
                    (verdict, confidence, explanation, method, datetime.now(timezone.utc).replace(tzinfo=None), claim_id))
    conn.commit()
    if qdrant is not None:
        qdrant.set_payload("claims", [claim["uuid"]], {"verdict": verdict})
    log.info("Claim %s: %s (%s sources, searched %s)", claim_id, verdict, len(sources), json.dumps(list(answer.searches)))
    return verdict


def unchecked(conn, claim_ids: Iterable[int]) -> list[int]:
    ids = sorted(set(claim_ids))
    if not ids:
        return []
    marks = ", ".join(["%s"] * len(ids))
    return [r["claim_id"] for r in _rows(conn, f"SELECT claim_id FROM claims WHERE claim_id IN ({marks}) AND verdict_at IS NULL",
                                         tuple(ids))]


def check_claims(conn, gemini: Gemini, qdrant: Qdrant | None, claim_ids: Iterable[int],
                 progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Fact-check the claims that weren't checked yet. Returns how many got each verdict."""
    say = progress or (lambda message: None)
    todo = unchecked(conn, claim_ids)
    out: dict[str, int] = {}
    for n, claim_id in enumerate(todo, 1):
        say(f"Fact-checking claim {n} of {len(todo)} with Gemini and Google Search.")
        verdict = check_claim(conn, gemini, qdrant, claim_id)
        if verdict:
            out[verdict] = out.get(verdict, 0) + 1
    return out
