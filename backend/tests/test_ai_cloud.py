"""LocalAI with the chat model elsewhere (LocalAI.cloud): the extension's "Cloud (Modal)" in-depth
analysis runs deep.py this way (stock Gemma on a Modal GPU, propaganda/gemma/modal_deep.py).

- Chat goes to the cloud base URL; embeddings stay on the local base URL.
- The cloud model's failures are plain sentences (starting / unreachable).
- Without chat_base_url nothing changes: one server for both.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from veritas import ai as ai_module
from veritas.ai import MODAL_STARTING, MODAL_UNREACHABLE, AiUnavailable, LocalAI, Prompt

CLOUD = "https://cloud.test/v1"
PROMPT = Prompt("probe", "v1", "Answer as JSON.", "{text}")


class Answer(BaseModel):
    ok: bool


class Servers:
    """The local server and the cloud one; records which host got which path."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.host, request.url.path))
        if request.url.path.endswith("/embeddings"):
            n = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1] * 768} for i in range(n)]})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            assert body["model"] == "chat" and body["response_format"]["type"] == "json_schema"
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "chat"}, {"id": "embedding"}]})
        return httpx.Response(404, json={})


@pytest.mark.parametrize("cloud", [False, True])
def test_chat_goes_to_the_cloud_and_embeddings_stay_local(cloud):
    servers = Servers()
    t = httpx.MockTransport(servers)
    ai = (LocalAI.cloud("http://local.test/v1", CLOUD, "gemma-4-26B-A4B-it", transport=t) if cloud
          else LocalAI("http://local.test/v1", transport=t))
    answer = ai.ask(PROMPT, Answer, text="hello")
    ai.embed_documents(["a post"])
    ai.embed_query("a query")
    ai.check()
    ai.close()
    assert answer.value.ok is True
    assert [h for h, p in servers.calls if p.endswith("/chat/completions")] == ["cloud.test" if cloud else "local.test"]
    assert [h for h, p in servers.calls if p.endswith("/embeddings")] == ["local.test", "local.test"]
    assert ("local.test", "/v1/models") in servers.calls
    if cloud:
        assert answer.model == "gemma-4-26B-A4B-it" and ai.engine == "modal" and ai.place == "ran in the cloud (Modal)"
        assert ("cloud.test", "/v1/models") in servers.calls   # check() looks at both
    else:
        assert ai.engine == "local" and ai.place == "ran on this computer"


def test_an_unknown_engine_is_refused():
    with pytest.raises(ValueError):
        LocalAI("http://ai.test/v1", engine="cloud")


def _cloud(handler, **kw) -> LocalAI:
    return LocalAI.cloud("http://local.test/v1", CLOUD, "m", transport=httpx.MockTransport(handler), cloud_wait=0, **kw)


def test_a_cold_start_is_waited_out(monkeypatch):
    """Modal answers a request held longer than ~150 s with a redirect: it is repeated until the model answers."""
    monkeypatch.setattr(ai_module, "MODAL_RETRY_S", 0)
    replies = iter([httpx.Response(303, headers={"location": "https://cloud.test/x"}), httpx.Response(503, json={}),
                    None, httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]})])

    def handler(request):
        r = next(replies)
        if r is None:
            raise httpx.ReadTimeout("slow", request=request)
        return r
    ai = LocalAI.cloud("http://local.test/v1", CLOUD, "m", transport=httpx.MockTransport(handler), cloud_wait=60)
    assert ai.ask(PROMPT, Answer, text="x").value.ok is True


def test_a_sleeping_cloud_model_says_it_is_starting():
    def slow(request):
        if request.url.host == "cloud.test":
            raise httpx.ReadTimeout("cold start", request=request)
        return httpx.Response(200, json={"data": []})
    ai = _cloud(slow)
    ai.check()  # asleep isn't a failure before the job: the job waits for it to start...
    with pytest.raises(AiUnavailable) as exc:
        ai.ask(PROMPT, Answer, text="x")
    assert str(exc.value) == MODAL_STARTING  # ...and a job that times out says so plainly

    loading = _cloud(lambda r: httpx.Response(503, json={}) if r.url.host == "cloud.test" else httpx.Response(200, json={}))
    loading.check()
    with pytest.raises(AiUnavailable, match="starting"):
        loading.ask(PROMPT, Answer, text="x")


def test_an_unreachable_cloud_model_is_a_plain_error():
    def refuse(request):
        if request.url.host == "cloud.test":
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(200, json={"data": []})
    with pytest.raises(AiUnavailable) as exc:
        _cloud(refuse).check()
    assert str(exc.value) == MODAL_UNREACHABLE
    with pytest.raises(AiUnavailable) as exc:
        _cloud(refuse).ask(PROMPT, Answer, text="x")
    assert str(exc.value) == MODAL_UNREACHABLE
