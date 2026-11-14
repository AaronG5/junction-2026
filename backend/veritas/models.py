"""The API contract, mirrored from frontend/src/api/types.ts.

Every /api response is one of these models. Python fields are snake_case; the JSON
uses the frontend's camelCase names (aliases). Two TypeScript details matter:
  - optional fields (`url?: string`) are LEFT OUT when empty, never sent as null;
  - nullable fields (`changePct: number | null`) are always sent, null included.
Unknown fields are rejected, so the two sides can't drift apart silently.
shared/fixtures/*.json must validate against these models (backend/tests/test_b1_contract.py).

"Same JSON" means equal once parsed: numbers compare by value (38 and 38.0 are the
same to the frontend), and key order doesn't matter.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, model_serializer
from pydantic.alias_generators import to_camel

Platform = Literal["Telegram", "Facebook", "Website"]
Verdict = Literal["true", "false", "misleading", "unverified"]
Stance = Literal["supports", "contradicts", "context", "neither"]


def _real_day(value: str) -> str:
    date.fromisoformat(value)  # rejects 2026-02-30, 2026-13-45, ...
    return value


def _clamp_score(value):
    """Similarity scores can overshoot by floating-point noise (1.0000001); clamp that,
    but let genuinely wrong values (1.5, -0.2) fail validation."""
    if isinstance(value, (int, float)) and 1 < value <= 1 + 1e-6:
        return 1.0
    if isinstance(value, (int, float)) and -1e-6 <= value < 0:
        return 0.0
    return value


# A calendar day, "YYYY-MM-DD" with ASCII digits, and a date that exists.
Day = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_real_day)]
Percent = Annotated[float, Field(ge=0, le=100)]
Score = Annotated[float, BeforeValidator(_clamp_score), Field(ge=0, le=1)]

ANALYZE_STEPS = (
    "Reading the post",
    "Making a text fingerprint (EmbeddingGemma)",
    "Finding similar posts in every language",
    "Checking the claims against sources",
    "Gemma reads the post",
)


class Contract(BaseModel):
    """Base for every contract model: camelCase JSON, no unknown fields, frozen."""

    # serialize_by_alias: even a plain model_dump() gives the frontend's names.
    # allow_inf_nan=False: infinity would otherwise be sent as null (which means "new").
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True,
        extra="forbid", frozen=True, allow_inf_nan=False,
    )
    # Field names (snake_case) that are optional in TypeScript: omitted when None.
    omit_if_none: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_optional(self, handler):
        data = handler(self)
        for name in self.omit_if_none:
            key = type(self).model_fields[name].alias or name
            if data.get(key, ...) is None:
                data.pop(key)
        return data

    def to_json(self) -> dict:
        """The JSON-ready dict exactly as the frontend expects it."""
        return self.model_dump(mode="json", by_alias=True)


# --------------------------------------------------------------------------- shared

class ErrorBody(Contract):
    """Every error answer: {"detail": "a plain-language sentence"}."""
    detail: str = Field(min_length=1)


class StatusCounts(Contract):
    all: int = Field(ge=0)
    true: int = Field(ge=0)
    false: int = Field(ge=0)
    misleading: int = Field(ge=0)
    unverified: int = Field(ge=0)


class Language(Contract):
    code: str
    name: str
    share: Percent


# ----------------------------------------------------------------------- narratives

class PostRef(Contract):
    omit_if_none = frozenset({"url"})
    id: str
    url: str | None = None
    text: str
    author: str
    where: str


class EdgePost(PostRef):
    note: str


class Cluster(Contract):
    id: int
    name: str
    size: int = Field(ge=0)
    change_pct: float | None  # null = new this week
    accounts: int = Field(ge=0)
    status: Verdict
    platforms: list[Platform]
    first_seen: Day
    last_seen: Day
    languages: list[Language]
    typical: list[PostRef]
    edge: list[EdgePost]


class MapPoint(Contract):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    cluster_id: int | None


class Run(Contract):
    date: Day
    posts: int = Field(ge=0)
    clusters: int = Field(ge=0)
    unclustered: int = Field(ge=0)


class NarrativesResponse(Contract):
    run: Run
    clusters: list[Cluster]
    total: int = Field(ge=0)
    counts: StatusCounts
    points: list[MapPoint]


# --------------------------------------------------------------------------- claims

class Evidence(Contract):
    omit_if_none = frozenset({"url"})
    source: str
    stance: Stance
    note: str
    url: str | None = None


class Claim(Contract):
    id: int
    text: str
    verdict: Verdict
    platforms: list[Platform]
    first_seen: Day
    last_seen: Day
    posts: int = Field(ge=0)
    languages: list[str]
    explanation: str
    evidence: list[Evidence]
    checked_by: str
    confidence: Score
    checked_at: Day
    cluster_id: int | None


class ClaimsResponse(Contract):
    claims: list[Claim]
    total: int = Field(ge=0)
    counts: StatusCounts


# -------------------------------------------------------------------------- sources

class Source(Contract):
    id: str
    name: str
    kind: str
    platform: Platform
    posts_30d: int = Field(alias="posts30d", ge=0)  # explicit: automatic conversion would give "posts30D"
    original_pct: Percent
    near_copy_pct: Percent
    main_topic: str
    open_flags: int = Field(ge=0)


class SourcesResponse(Contract):
    sources: list[Source]


class ChannelStats(Contract):
    posts_per_day: float = Field(ge=0)
    original_pct: Percent
    near_copy_pct: Percent
    state_media_link_pct: Percent


class DailyCount(Contract):
    date: Day
    posts: int = Field(ge=0)


class TopicShare(Contract):
    name: str
    pct: Percent


class Upstream(Contract):
    name: str
    kind: str
    posts: int = Field(ge=0)
    typical_delay: str


class RecentPost(Contract):
    id: str
    url: str
    text: str
    published_at: str
    views: int = Field(ge=0)
    verdict: Verdict


class ChannelReport(Contract):
    id: str
    name: str
    platform: Platform
    type_label: str
    subtitle: str
    based_on: str
    stats: ChannelStats
    daily: list[DailyCount]
    topics: list[TopicShare]
    upstream: list[Upstream]
    recent: list[RecentPost]


# -------------------------------------------------------------------------- analyze

class TextSegment(Contract):
    omit_if_none = frozenset({"flag"})
    text: str
    flag: str | None = None


class ChainNode(Contract):
    omit_if_none = frozenset({"relation", "is_this_post"})
    name: str
    kind: str
    language: str
    time: str
    relation: str | None = None
    is_this_post: bool | None = None


class ReportPost(Contract):
    author: str
    source_id: str | None
    where: str
    original: list[TextSegment]
    original_language: str
    translation: list[TextSegment]


class VerdictSummary(Contract):
    verdict: Verdict
    summary: str


class Spread(Contract):
    copies: int = Field(ge=0)
    languages: int = Field(ge=0)
    hours: float = Field(ge=0)


class Earliest(Contract):
    name: str
    note: str


class ClaimSummary(Contract):
    id: int
    text: str
    verdict: Verdict
    explanation: str
    evidence_summary: str
    seen_summary: str


class SimilarPost(Contract):
    language: str
    author: str
    where: str
    text: str
    relation: str
    similarity: Score


class CategoryScore(Contract):
    name: str
    score: Score
    method: str


class Technique(Contract):
    name: str
    detail: str


class Reading(Contract):
    tone: str
    intent: str
    techniques: list[Technique]
    model: str


class ClusterCard(Contract):
    id: int
    name: str
    summary: str
    languages: list[Language]


class PostReport(Contract):
    url: str
    analyzed_at: str
    answered_from: str
    post: ReportPost
    verdict: VerdictSummary
    manipulation: float = Field(ge=0, le=10)
    spread: Spread
    earliest: Earliest
    chain: list[ChainNode]
    more_copies: int = Field(ge=0)
    claims: list[ClaimSummary]
    similar: list[SimilarPost]
    categories: list[CategoryScore]
    reading: Reading
    cluster: ClusterCard | None  # null: the post fits no known narrative yet


class AnalysisJob(Contract):
    """POST /api/analyze and GET /api/analyze/{id}: an analysis the screen polls.
    step: index into ANALYZE_STEPS while running; 5 when done."""
    id: str
    status: Literal["queued", "running", "done", "failed"]
    step: int = Field(ge=0, le=5)
    report: PostReport | None
    error: str | None
    error_status: int | None


# ------------------------------------------------------------------ landing stories

class Trend(Contract):
    posts: int = Field(ge=0)
    change_pct: float | None


class StoryPost(Contract):
    omit_if_none = frozenset({"url"})
    author: str
    where: str
    text: str
    translation: str
    url: str | None = None


class StoryClaim(Contract):
    headline: str
    text: str
    verdict: Verdict
    explanation: str
    evidence: str


class OriginStep(Contract):
    meta: str
    name: str
    kind: str


class Origin(Contract):
    headline: str
    chain: list[OriginStep]


class LanguageCount(Contract):
    name: str
    count: int = Field(ge=0)


class Travel(Contract):
    headline: str
    copies: int = Field(ge=0)
    accounts: int = Field(ge=0)
    time_label: str
    languages: list[LanguageCount]


class StoryNarrative(Contract):
    headline: str
    name: str
    stats: str


class Story(Contract):
    id: str
    platform: Platform
    status: Verdict
    first_seen: Day
    last_seen: Day
    topic: str
    trend: Trend
    headline: str
    sub: str
    post: StoryPost
    claim: StoryClaim
    origin: Origin
    travel: Travel
    narrative: StoryNarrative


class TrendingResponse(Contract):
    stories: list[Story]


# --------------------------------------------------------------------------- system

class Upgrade(Contract):
    model: str
    download_gb: float = Field(ge=0)
    note: str


class AiStatus(Contract):
    running: bool
    chat_model: str
    chat_model_note: str
    embedding_model: str
    embedding_note: str
    hardware: str
    hardware_note: str
    upgrade: Upgrade | None


class Service(Contract):
    name: str
    role: str
    address: str
    running: bool


class SystemStatus(Contract):
    ai: AiStatus
    services: list[Service]


# Response models by the name used in shared/fixtures/manifest.json.

class AppSettings(Contract):
    """GET/PUT /api/settings: shared settings stored in the database (B7)."""
    data_mode: Literal["central", "hybrid", "local"]
    share_analyses: StrictBool  # "no" or 0 is refused, not read as false


RESPONSE_MODELS: dict[str, type[Contract]] = {
    "Error": ErrorBody,
    "NarrativesResponse": NarrativesResponse,
    "ClaimsResponse": ClaimsResponse,
    "SourcesResponse": SourcesResponse,
    "ChannelReport": ChannelReport,
    "AnalysisJob": AnalysisJob,  # carries the PostReport
    "AppSettings": AppSettings,
    "Story": Story,
    "TrendingResponse": TrendingResponse,
    "SystemStatus": SystemStatus,
}
