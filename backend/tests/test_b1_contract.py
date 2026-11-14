"""B1: the backend's models match the frontend's contract (shared/fixtures)."""
from __future__ import annotations

import copy
import json
import re

import pytest
from pydantic import ValidationError

from conftest import FIXTURES
from veritas.config import ROOT
from veritas.models import (
    ANALYZE_STEPS, RESPONSE_MODELS, AiStatus, ChainNode, Cluster, EdgePost, Evidence, Language, PostRef, ReportPost,
    SimilarPost, Source, StatusCounts, TextSegment, Trend,
)

MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
ENTRIES = MANIFEST["fixtures"]


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def all_keys(value, path="$"):
    """Every (path, key) in a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield path, k
            yield from all_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from all_keys(v, f"{path}[{i}]")


# ----------------------------------------------------------------- the fixture set

def test_manifest_and_folder_agree() -> None:
    listed = {e["file"] for e in ENTRIES}
    present = {p.name for p in FIXTURES.glob("*.json")} - {"manifest.json"}
    assert listed == present


def test_every_response_model_has_a_fixture() -> None:
    assert {e["model"] for e in ENTRIES} == set(RESPONSE_MODELS)


def test_manifest_entries_describe_a_real_api_call() -> None:
    for e in ENTRIES:
        assert e["method"] in ("GET", "POST")
        assert e["path"].startswith("/api/")
        assert all(isinstance(v, str) for v in e["query"].values())
        assert e["replay"] in ("exact", "shape")
        assert (e["status"] == 200) == (e["model"] != "Error"), e["file"]


def test_error_fixtures_are_plain_sentences() -> None:
    errors = [e for e in ENTRIES if e["model"] == "Error"]
    assert sorted(e["status"] for e in errors) == [400, 400, 404, 404]
    for e in errors:
        detail = load(e["file"])["detail"]
        assert detail[0].isupper() and detail.endswith("."), detail


def test_only_live_or_ai_answers_are_compared_by_shape() -> None:
    assert sorted(e["file"] for e in ENTRIES if e["replay"] == "shape") == ["analyze-kanalas_b-4521.json", "system.json"]


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["file"])
def test_fixture_validates_against_its_model(entry: dict) -> None:
    RESPONSE_MODELS[entry["model"]].model_validate(load(entry["file"]))


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["file"])
def test_fixture_round_trips_to_identical_json(entry: dict) -> None:
    """What the backend would send for this data is exactly what the frontend expects."""
    data = load(entry["file"])
    assert RESPONSE_MODELS[entry["model"]].model_validate(data).to_json() == data


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["file"])
def test_fixture_keys_are_camel_case(entry: dict) -> None:
    for path, key in all_keys(load(entry["file"])):
        assert re.fullmatch(r"[a-z][a-zA-Z0-9]*", key), f"{path}: {key!r} is not camelCase"


# ------------------------------------------------------------ drift is caught

@pytest.mark.parametrize("where", ["top", "nested"])
def test_an_unknown_field_is_rejected(where: str) -> None:
    data = copy.deepcopy(load("narratives-7d.json"))
    target = data if where == "top" else data["clusters"][0]
    target["brandNewField"] = 1
    with pytest.raises(ValidationError, match="brandNewField"):
        RESPONSE_MODELS["NarrativesResponse"].model_validate(data)


def test_a_missing_field_is_rejected() -> None:
    data = copy.deepcopy(load("source-kanalas_b.json"))
    del data["stats"]["nearCopyPct"]
    with pytest.raises(ValidationError, match="nearCopyPct"):
        RESPONSE_MODELS["ChannelReport"].model_validate(data)


@pytest.mark.parametrize("field,value", [("status", "fake"), ("platforms", ["Twitter"]), ("firstSeen", "14/11/2026"), ("size", -1)])
def test_wrong_values_are_rejected(field: str, value) -> None:
    data = copy.deepcopy(load("narratives-7d.json"))
    data["clusters"][0][field] = value
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["NarrativesResponse"].model_validate(data)


def test_snake_case_input_would_not_reach_the_frontend_unchanged() -> None:
    """Python code may build models with snake_case names, but the JSON always uses camelCase."""
    src = Source(id="x", name="x", kind="x", platform="Telegram", posts_30d=5, original_pct=1, near_copy_pct=2, main_topic="t", open_flags=0)
    out = src.to_json()
    assert "posts30d" in out and "posts30D" not in out and "posts_30d" not in out
    assert "nearCopyPct" in out and "near_copy_pct" not in out


# ------------------------------------------------- optional vs. nullable fields

def test_optional_fields_are_left_out_when_empty() -> None:
    assert "url" not in PostRef(id="1", text="t", author="a", where="w").to_json()
    assert "flag" not in TextSegment(text="t").to_json()
    node = ChainNode(name="n", kind="k", language="LT", time="11:00").to_json()
    assert "relation" not in node and "isThisPost" not in node


def test_optional_fields_are_sent_when_set() -> None:
    assert PostRef(id="1", url="https://t.me/x/1", text="t", author="a", where="w").to_json()["url"] == "https://t.me/x/1"
    assert ChainNode(name="n", kind="k", language="LT", time="1", is_this_post=True).to_json()["isThisPost"] is True


def test_nullable_fields_are_always_sent_even_as_null() -> None:
    cluster = Cluster(
        id=1, name="n", size=1, change_pct=None, accounts=1, status="false", platforms=["Telegram"],
        first_seen="2026-11-01", last_seen="2026-11-02", languages=[], typical=[], edge=[],
    ).to_json()
    assert "changePct" in cluster and cluster["changePct"] is None


def test_bounds() -> None:
    with pytest.raises(ValidationError):
        Language(code="lt", name="Lithuanian", share=101)
    with pytest.raises(ValidationError):
        StatusCounts(all=-1, true=0, false=0, misleading=0, unverified=0)


# Fullwidth digits (look like 2026-11-14) and a trailing newline, built with chr() to stay visible.
FULLWIDTH_DAY = "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in "2026-11-14")


@pytest.mark.parametrize("day", ["2026-02-30", "2026-13-45", FULLWIDTH_DAY, "2026-11-14" + chr(10), "14/11/2026", ""])
def test_days_must_be_real_dates_in_ascii_digits(day: str) -> None:
    data = copy.deepcopy(load("narratives-7d.json"))
    data["clusters"][0]["firstSeen"] = day
    with pytest.raises(ValidationError):
        RESPONSE_MODELS["NarrativesResponse"].model_validate(data)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_infinity_and_nan_are_refused(value: float) -> None:
    """Infinity would otherwise be sent as null, which the screen reads as "new this week"."""
    with pytest.raises(ValidationError):
        Trend(posts=1, change_pct=value)


def _similar(score: float) -> SimilarPost:
    return SimilarPost(language="LT", author="a", where="w", text="t", relation="Copy", similarity=score)


def test_tiny_floating_point_overshoot_in_scores_is_clamped() -> None:
    assert _similar(1.0000001).similarity == 1.0
    assert _similar(-0.0000001).similarity == 0.0


@pytest.mark.parametrize("score", [1.5, -0.2, 1.01])
def test_wrong_scores_still_fail(score: float) -> None:
    with pytest.raises(ValidationError):
        _similar(score)


def test_plain_model_dump_matches_the_api_shape() -> None:
    dumped = ChainNode(name="n", kind="k", language="LT", time="1").model_dump()
    assert dumped == {"name": "n", "kind": "k", "language": "LT", "time": "1"}


# Fields the fixtures never exercise in this form: pin them explicitly.

def test_evidence_and_edge_post_links_are_sent_when_known() -> None:
    assert Evidence(source="s", stance="contradicts", note="n", url="https://lrt.lt/x").to_json()["url"] == "https://lrt.lt/x"
    assert "url" not in Evidence(source="s", stance="supports", note="n").to_json()
    assert EdgePost(id="1", url="https://t.me/x/1", text="t", author="a", where="w", note="n").to_json()["url"] == "https://t.me/x/1"


def test_unknown_author_source_is_sent_as_null() -> None:
    post = ReportPost(author="a", source_id=None, where="w", original=[], original_language="Lithuanian", translation=[]).to_json()
    assert "sourceId" in post and post["sourceId"] is None


def test_no_upgrade_is_sent_as_null() -> None:
    ai = AiStatus(running=False, chat_model="m", chat_model_note="n", embedding_model="e", embedding_note="n",
                  hardware="h", hardware_note="n", upgrade=None).to_json()
    assert "upgrade" in ai and ai["upgrade"] is None


def test_new_story_trend_is_sent_as_null() -> None:
    assert Trend(posts=3, change_pct=None).to_json() == {"posts": 3, "changePct": None}


# ----------------------------------------------------- constants shared with the UI

def test_analyze_steps_match_the_frontend() -> None:
    types_ts = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    block = re.search(r"ANALYZE_STEPS = \[(.*?)\] as const", types_ts, re.S)
    assert block, "ANALYZE_STEPS not found in types.ts"
    assert tuple(re.findall(r'"([^"]+)"', block.group(1))) == ANALYZE_STEPS


def test_fixtures_are_marked_as_generated() -> None:
    assert "export:fixtures" in MANIFEST["note"]
    assert MANIFEST["today"] == MANIFEST["generatedAt"][:10]
