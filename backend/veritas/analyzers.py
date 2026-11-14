"""The analyzers: what Gemma is asked about a post (docs/analysis-cycle.md).

  extract_claims   the checkable factual claims, each with the post's own words
  read_post        tone, intent, manipulation techniques with the flagged wording, a 0-10 score
  confirm_category does the post really belong to a category similarity search suggested?
  translate        the post in English (or another language) for display

Each returns an Answer: the value plus the model and prompt version that made it,
so results from an old prompt can be found and re-run.

Wording Gemma "quotes" from the post is checked against the post: a technique
whose flagged wording isn't in the post is dropped (Gemma sometimes paraphrases
or invents), and a claim's quote is cleared. Matching ignores letter case and
runs of spaces; what's returned is the post's own text at that place.
"""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .ai import Answer, LocalAI, load_prompt

Short = Annotated[str, Field(max_length=500)]


class _Strict(BaseModel):
    # strict: "yes" is not a boolean and "7" is not a number; unknown keys are refused.
    model_config = ConfigDict(extra="forbid", strict=True)


# --------------------------------------------------------------------------- answer schemas (sent to Gemma)

class ExtractedClaim(_Strict):
    claim: Short
    quote: Short


class ClaimsAnswer(_Strict):
    claims: list[ExtractedClaim] = Field(max_length=5)


class TechniqueAnswer(_Strict):
    name: Annotated[str, Field(max_length=60)]
    wording: Short
    detail: Short


class ReadingAnswer(_Strict):
    tone: Annotated[str, Field(max_length=40)]
    intent: Short
    techniques: list[TechniqueAnswer] = Field(max_length=10)
    manipulation: int = Field(ge=0, le=10)


class CategoryAnswer(_Strict):
    matches: bool
    confidence: float = Field(ge=0, le=1)
    reason: Short


class TranslationAnswer(_Strict):
    source_language: Annotated[str, Field(max_length=40)]
    translation: Annotated[str, Field(max_length=20_000)]


# --------------------------------------------------------------------------- wording checks

_SPACES = re.compile(r"\s+")


def find_wording(text: str, wording: str) -> str | None:
    """The post's own words that match `wording` (ignoring case and runs of spaces),
    or None when the post doesn't contain them."""
    words = _SPACES.split(wording.strip())
    if not words or words == [""]:
        return None
    pattern = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0) if m else None


# --------------------------------------------------------------------------- analyzers

def extract_claims(ai: LocalAI, text: str, language: str = "unknown") -> Answer[ClaimsAnswer]:
    answer = ai.ask(load_prompt("claims"), ClaimsAnswer, text=text, language=language)
    claims = [ExtractedClaim(claim=c.claim.strip(), quote=find_wording(text, c.quote) or "")
              for c in answer.value.claims if c.claim.strip()]
    return Answer(ClaimsAnswer(claims=claims), answer.model, answer.prompt)


def read_post(ai: LocalAI, text: str, language: str = "unknown") -> Answer[ReadingAnswer]:
    answer = ai.ask(load_prompt("reading"), ReadingAnswer, text=text, language=language)
    kept = []
    for t in answer.value.techniques:
        found = find_wording(text, t.wording)
        if found is not None:
            kept.append(TechniqueAnswer(name=t.name.strip(), wording=found, detail=t.detail.strip()))
    value = answer.value.model_copy(update={"techniques": kept})
    return Answer(value, answer.model, answer.prompt)


def confirm_category(ai: LocalAI, text: str, category: str, description: str,
                     language: str = "unknown") -> Answer[CategoryAnswer]:
    return ai.ask(load_prompt("category"), CategoryAnswer, text=text, category=category,
                  description=description, language=language)


def translate(ai: LocalAI, text: str, target: str = "English") -> Answer[TranslationAnswer]:
    return ai.ask(load_prompt("translate"), TranslationAnswer, text=text, target=target, max_tokens=4096)
