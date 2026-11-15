"""Extension API + narrative engine, with a fake embedder and a fake judge (no AI, no network, no bank files)."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from conftest import make_client
from veritas import narrative


def unit(*xs):
    v = np.zeros(768, np.float32)
    for i, x in enumerate(xs):
        v[i] = x
    return v / np.linalg.norm(v)


TOPIC = {"puppet": unit(1, 0.05), "nvidia": unit(0, 1, 0.05), "weather": unit(0, 0, 0, 1)}


def fake_embed(texts):
    out = []
    for t in texts:
        t = t.lower()
        k = "puppet" if "puppet" in t else "nvidia" if "nvidia" in t else "weather"
        out.append(TOPIC[k].tolist())
    return out


def make_bank(tmp_path):
    root = tmp_path / "bank"
    for shard, rows, vecs in [
        ("euvsdisinfo", [{"claim_id": "c1", "narrative_id": "n1", "text": "Kyiv is an American puppet."}], [unit(1, 0.04)]),
        ("rt", [{"claim_id": "r1", "narrative_id": "rn1", "text": "Ukraine is run from Washington.", "n_mentions": 253,
                 "date_first": "2024-01-07", "date_last": "2024-10-02"}], [unit(1, 0.06)]),
    ]:
        d = root / "claims" / shard
        d.mkdir(parents=True)
        (d / "claims.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        np.save(d / "emb.npy", np.stack(vecs))
    tg = root / "sources" / "telegram"
    tg.mkdir(parents=True)
    (tg / "meta.jsonl").write_text(json.dumps({"channel": "two_majors", "url": "https://t.me/two_majors/1",
                                               "date": "2026-11-13T15:00:00Z", "text": "Nvidia CEO left in a CIA operation"}))
    np.save(tg / "emb.npy", np.stack([unit(0, 1, 0.04)]))
    (root / "manifest.json").write_text(json.dumps({"thresholds": {"copy_strong": 0.95}}))
    return narrative.Bank.load(root)


@pytest.fixture
def engine(tmp_path):
    calls = []

    def judge(text, claims):
        calls.append((text, claims))
        return {"ppg": 1, "matched": 1, "reason": "Asserts the puppet claim.", "techniques": [["LL", "puppet regime"]]}

    eng = narrative.Engine(make_bank(tmp_path), fake_embed, judge, workers=1, feedback_path=tmp_path / "fb.jsonl")
    eng.calls = calls
    narrative.set_engine(eng)
    yield eng
    narrative.set_engine(None)


def items(*pairs):
    return {"client": "test", "items": [{"id": i, "author": a, "text": t} for i, a, t in pairs]}


def wait_done(client, ids, tries=50):
    for _ in range(tries):
        r = client.get("/api/ext/result", params={"ids": ",".join(ids)}).json()["results"]
        if all(x["status"] != "pending" for x in r):
            return r
        time.sleep(0.05)
    raise AssertionError("still pending")


def test_every_item_answered_and_judge_runs_on_candidates(settings, engine):
    c = make_client(settings)
    body = items(("1", "alice", "The Kyiv regime is a puppet of Washington, everyone knows it."),
                 ("2", "bob", "Lovely weather in Helsinki today, going for a long walk."),
                 ("bad id!", "x", "whatever text long enough here"))
    r = c.post("/api/ext/batch", json=body)
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x["id"] for x in res] == ["1", "2", "bad id!"]
    assert res[0]["status"] in ("pending", "done") and res[0]["content"]["narrative"]["text"].startswith("Kyiv")
    assert res[0]["content"]["state_media_echo"][0]["n_aired"] == 253
    assert res[2]["status"] == "error"
    done = wait_done(c, ["1", "2"])
    assert done[0]["content"]["label"] == "potentially_propaganda"
    assert done[0]["content"]["techniques"][0]["name"] == "loaded language"
    assert done[1]["content"]["label"] == "none"
    assert len(engine.calls) == 1                      # the weather post never reached the judge


def test_source_match_and_coordination(settings, engine):
    c = make_client(settings)
    txt = "Nvidia CEO left his position as part of a CIA operation, sources say."
    res = c.post("/api/ext/batch", json=items(*[(str(10 + k), f"acct{k}", txt) for k in range(5)])).json()["results"]
    assert res[0]["source_match"]["strong"] is True
    assert res[0]["source_match"]["telegram"][0]["channel"] == "two_majors"
    last = res[-1]["coordination"]
    assert last["label"] == "coordinated" and last["n_similar_accounts"] == 4


def test_judge_failure_keeps_fast_fields(settings, tmp_path):
    def broken(text, claims):
        raise RuntimeError("modal down")
    eng = narrative.Engine(make_bank(tmp_path), fake_embed, broken, workers=1)
    narrative.set_engine(eng)
    try:
        c = make_client(settings)
        c.post("/api/ext/batch", json=items(("7", "a", "Kyiv is a puppet state run by others.")))
        done = wait_done(c, ["7"])
        assert done[0]["content"]["label"] == "none" and "unavailable" in done[0]["content"]["reason"]
        assert done[0]["content"]["narrative"] is not None
    finally:
        narrative.set_engine(None)


def test_embedding_down_answers_with_errors(settings, tmp_path):
    def down(texts):
        raise RuntimeError("llama-swap not running")
    narrative.set_engine(narrative.Engine(make_bank(tmp_path), down, None))
    try:
        res = make_client(settings).post("/api/ext/batch", json=items(("8", "a", "Kyiv is a puppet state, clearly."))).json()["results"]
        assert res[0]["status"] == "error"
    finally:
        narrative.set_engine(None)


def test_feedback_and_validation(settings, engine, tmp_path):
    c = make_client(settings)
    assert c.post("/api/ext/feedback", json={"tweet_id": "1", "verdict_shown": "source_match+potentially_propaganda",
                                             "user_says": "wrong"}).json() == {"ok": True}
    assert json.loads((tmp_path / "fb.jsonl").read_text())["user_says"] == "wrong"
    assert c.post("/api/ext/feedback", json={"tweet_id": "1", "verdict_shown": "x", "user_says": "maybe"}).status_code in (400, 422)
    too_many = items(*[(str(i), "a", "text text text text text") for i in range(40)])
    assert c.post("/api/ext/batch", json=too_many).status_code in (400, 422)


def test_missing_bank_still_answers(settings, tmp_path):
    narrative.set_engine(narrative.Engine(narrative.Bank.load(tmp_path / "nope"), fake_embed, None))
    try:
        res = make_client(settings).post("/api/ext/batch", json=items(("9", "a", "Kyiv is a puppet state, clearly."))).json()["results"]
        assert res[0]["status"] == "done" and res[0]["source_match"] is None
    finally:
        narrative.set_engine(None)


def test_status_reports_bank_thresholds_and_judge(settings, engine):
    s = make_client(settings).get("/api/ext/status").json()
    assert s["available"] is True and s["reason"] is None
    assert (s["claims"], s["echoClaims"], s["telegramPosts"]) == (1, 1, 1)
    assert s["thresholds"]["copy_strong"] == 0.95 and "candidate_gate" in s["thresholds"]
    assert s["judge"] is True


def test_status_without_judge_or_bank(settings, tmp_path):
    narrative.set_engine(narrative.Engine(narrative.Bank.load(tmp_path / "nope"), fake_embed, None))
    try:
        s = make_client(settings).get("/api/ext/status").json()
        assert s["available"] is True and s["judge"] is False and s["telegramPosts"] == 0 and s["embeddingModel"] is None
    finally:
        narrative.set_engine(None)


def test_author_rule_uses_share_not_just_count(settings, tmp_path):
    """3 Telegram copies among 3 posts flags; 3 copies among 30 posts (a busy news account) does not."""
    eng = narrative.Engine(make_bank(tmp_path), fake_embed, None)
    narrative.set_engine(eng)
    try:
        c = make_client(settings)
        copy = "Nvidia CEO left his position as part of a CIA operation, sources say."
        res = c.post("/api/ext/batch", json=items(*[(f"r{k}", "relaybot", copy + f" #{k}") for k in range(3)])).json()["results"]
        assert any("copy pro-Kremlin Telegram posts" in r for r in res[-1]["coordination"]["reasons"])
        news = [(f"n{k}", "busynews", f"Lovely weather report number {k} from Helsinki, a calm and sunny day.") for k in range(27)]
        c.post("/api/ext/batch", json=items(*news[:20]))
        c.post("/api/ext/batch", json=items(*news[20:]))
        res = c.post("/api/ext/batch", json=items(*[(f"m{k}", "busynews", copy + f" !{k}") for k in range(3)])).json()["results"]
        assert not any("copy pro-Kremlin Telegram posts" in r for r in res[-1]["coordination"]["reasons"])
    finally:
        narrative.set_engine(None)
