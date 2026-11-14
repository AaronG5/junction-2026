"""B1: the backend follows the same filter and link rules as the frontend
(shared/cases/*.json, also run by the frontend's Vitest suite)."""
from __future__ import annotations

import json

import pytest

from conftest import CASES
from veritas.filters import (
    DateRange, matches_platform, matches_query, matches_status, overlaps, range_from_params, resolve_range, shift_days,
)
from veritas.links import is_supported_link

FILTERS = json.loads((CASES / "filters.json").read_text(encoding="utf-8"))
LINKS = json.loads((CASES / "links.json").read_text(encoding="utf-8"))
TODAY = FILTERS["today"]


@pytest.mark.parametrize("case", FILTERS["resolveRange"], ids=lambda c: c["name"])
def test_resolve_range(case: dict) -> None:
    f = case["filters"]
    assert resolve_range(f["range"], f["from"], f["to"], TODAY).as_dict() == case["expected"]


@pytest.mark.parametrize("case", FILTERS["overlaps"], ids=lambda c: c["name"])
def test_overlaps(case: dict) -> None:
    r = case["range"]
    assert overlaps(case["firstSeen"], case["lastSeen"], DateRange(r["from"], r["to"], r["invalid"])) is case["expected"]


@pytest.mark.parametrize("case", FILTERS["matchesPlatform"], ids=lambda c: f"{c['platforms']}-{c['filter']}")
def test_matches_platform(case: dict) -> None:
    assert matches_platform(case["platforms"], case["filter"]) is case["expected"]


@pytest.mark.parametrize("case", FILTERS["matchesStatus"], ids=lambda c: f"{c['verdict']}-{c['filter']}")
def test_matches_status(case: dict) -> None:
    assert matches_status(case["verdict"], case["filter"]) is case["expected"]


@pytest.mark.parametrize("case", FILTERS["matchesQuery"], ids=lambda c: c["name"])
def test_matches_query(case: dict) -> None:
    assert matches_query(case["text"], case["query"]) is case["expected"]


@pytest.mark.parametrize("case", FILTERS["filtersToParams"], ids=lambda c: c["name"])
def test_what_the_frontend_sends_means_the_same_range_here(case: dict) -> None:
    """The frontend turns filters into from/to parameters; reading those parameters
    back must give the same date range the frontend meant."""
    f, params = case["filters"], case["expected"]
    meant = resolve_range(f["range"], f["from"], f["to"], TODAY)
    received = range_from_params(params.get("from"), params.get("to"))
    if meant.invalid:
        assert received == DateRange()  # nothing sent = no date filter, same as "invalid"
    else:
        assert received == meant


@pytest.mark.parametrize("url", LINKS["accept"])
def test_supported_links(url: str) -> None:
    assert is_supported_link(url)


@pytest.mark.parametrize("url", LINKS["reject"])
def test_unsupported_links(url: str) -> None:
    assert not is_supported_link(url)


def test_shift_days_crosses_month_and_year_boundaries() -> None:
    assert shift_days("2026-11-01", -1) == "2026-10-31"
    assert shift_days("2026-01-01", -1) == "2025-12-31"
    assert shift_days("2028-02-28", 1) == "2028-02-29"


def test_unknown_range_is_an_error() -> None:
    with pytest.raises(ValueError, match="Unknown range"):
        resolve_range("365", "", "", TODAY)


def test_a_reversed_range_from_parameters_filters_nothing() -> None:
    r = range_from_params("2026-12-01", "2026-11-01")
    assert r.invalid and overlaps("2000-01-01", "2000-01-02", r)
