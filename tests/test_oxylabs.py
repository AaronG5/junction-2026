"""Oxylabs client: retries and error mapping against a mocked transport.

Uses its own dummy key; the real key is never read here.
"""
import email.message
import io
import json
import logging
import os
import socket
import unittest
import urllib.error
from unittest import mock

from scrapers import oxylabs
from scrapers.oxylabs import (AuthenticationError, HttpResponse, MissingApiKeyError,
                              RateLimitError, ValidationError, WebApiClient, WebApiError)

logging.getLogger("scrapers").addHandler(logging.NullHandler())

DUMMY_KEY = "dummy-test-key-not-real-0000"


def ok(status=200, results=None, request_id="req-ok"):
    body = {"state": "done", "results": results or [], "metadata": {"request_id": request_id}}
    return HttpResponse(status, json.dumps(body).encode())


def problem(status, title, detail="", pointers=(), request_id="req-err"):
    body = {"status": status, "title": title, "detail": detail,
            "errors": [{"pointer": p, "detail": "invalid"} for p in pointers],
            "metadata": {"request_id": request_id}}
    return HttpResponse(status, json.dumps(body).encode())


class FakeTransport:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "payload": json.loads(body), "timeout": timeout})
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def make_client(*steps):
    transport = FakeTransport(*steps)
    sleeps = []
    client = WebApiClient(DUMMY_KEY, transport=transport, sleep=sleeps.append, jitter=lambda: 0.5)
    return client, transport, sleeps


class RequestTests(unittest.TestCase):
    def test_search_sends_bearer_auth_and_returns_request_id(self):
        client, transport, _ = make_client(ok(results=[{"url": "https://example.com"}], request_id="r1"))
        result = client.search("baltic news", max_results=3)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://webapi.oxylabs.io/v1/search")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {DUMMY_KEY}")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["timeout"], 150)
        self.assertEqual(call["payload"], {"query": "baltic news", "max_results": 3})
        self.assertEqual(result.request_id, "r1")
        self.assertEqual(result.results, [{"url": "https://example.com"}])

    def test_scrape_returns_content_by_format(self):
        client, transport, _ = make_client(ok(results=[{"html": "<p>hi</p>", "metadata": {}}]))
        result = client.scrape("https://t.me/s/infalt", output=["html"], location="ru")
        self.assertEqual(transport.calls[0]["url"], "https://webapi.oxylabs.io/v1/scrape")
        self.assertEqual(transport.calls[0]["payload"],
                         {"url": "https://t.me/s/infalt", "output": ["html"], "location": "RU"})
        self.assertEqual(result.text("html"), "<p>hi</p>")
        self.assertEqual(result.text("markdown"), "")

    def test_any_2xx_is_success(self):
        for status in (201, 202, 204):
            client, transport, _ = make_client(ok(status=status))
            client.search("q")
            self.assertEqual(len(transport.calls), 1)

    def test_input_is_validated_before_sending(self):
        client, transport, _ = make_client(ok())
        with self.assertRaises(ValueError):
            client.search("x" * 2049)
        with self.assertRaises(ValueError):
            client.search("q", location="Russia")
        with self.assertRaises(ValueError):
            client.scrape("t.me/s/infalt")
        with self.assertRaises(ValueError):
            client.scrape("https://example.com", output=["screenshot"])
        self.assertEqual(transport.calls, [])
        client.search("q", max_results=50)
        self.assertEqual(transport.calls[0]["payload"]["max_results"], 20)


class YouTubeEndpointTests(unittest.TestCase):
    def test_paths_and_payloads(self):
        client, transport, _ = make_client(
            ok(results=[{"json": {"id": "UCx", "videos": []}}], request_id="r1"), ok(), ok())
        result = client.youtube("channel", "VaidasZemaitisLekstutis", limit=20)
        client.youtube("subtitles", "2aZ_aDxebwI", language_code="lt", subtitle_origin="auto_generated")
        client.youtube("metadata", "2aZ_aDxebwI", location="lt")
        base = "https://webapi.oxylabs.io/v1/scrape/media/youtube/"
        self.assertEqual([c["url"] for c in transport.calls],
                         [base + "channel", base + "subtitles", base + "metadata"])
        self.assertEqual([c["payload"] for c in transport.calls], [
            {"query": "@VaidasZemaitisLekstutis", "output": ["json"], "limit": 20},
            {"query": "2aZ_aDxebwI", "language_code": "lt", "subtitle_origin": "auto_generated"},
            {"query": "2aZ_aDxebwI", "location": "LT"},
        ])
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], f"Bearer {DUMMY_KEY}")
        self.assertEqual((result.request_id, result.json), ("r1", {"id": "UCx", "videos": []}))

    def test_bad_input_sends_nothing(self):
        client, transport, _ = make_client(ok())
        bad = [("download", "2aZ_aDxebwI", {}),
               ("channel", "https://www.youtube.com/@x", {}),
               ("channel", "@two words", {}),
               ("channel", "@x", {"limit": -1}),
               ("metadata", "https://youtu.be/2aZ_aDxebwI", {}),
               ("subtitles", "short", {}),
               ("subtitles", "2aZ_aDxebwI", {"language_code": "LT"}),
               ("subtitles", "2aZ_aDxebwI", {"subtitle_origin": "translated"}),
               ("metadata", "2aZ_aDxebwI", {"language_code": "lt"}),
               ("metadata", "2aZ_aDxebwI", {"limit": 5})]
        for endpoint, query, params in bad:
            with self.assertRaises(ValueError, msg=(endpoint, query, params)):
                client.youtube(endpoint, query, **params)
        self.assertEqual(transport.calls, [])

    def test_retries_and_errors_are_the_same_as_for_scrape(self):
        client, transport, _ = make_client(problem(503, "UNAVAILABLE"), ok(request_id="r2"))
        self.assertEqual(client.youtube("metadata", "2aZ_aDxebwI").request_id, "r2")
        self.assertEqual(len(transport.calls), 2)
        client, transport, _ = make_client(problem(400, "VALIDATION_ERROR", pointers=["/language_code"]))
        with self.assertRaises(ValidationError):
            client.youtube("subtitles", "2aZ_aDxebwI", language_code="xx")
        self.assertEqual(len(transport.calls), 1)
        client, _, _ = make_client(problem(401, "UNAUTHORIZED"))
        with self.assertRaises(AuthenticationError):
            client.youtube("channel", "@x")

    def test_json_is_none_without_results(self):
        client, _, _ = make_client(ok())
        self.assertIsNone(client.youtube("subtitles", "2aZ_aDxebwI").json)


class ErrorMappingTests(unittest.TestCase):
    def test_401_raises_authentication_error_without_retry(self):
        client, transport, sleeps = make_client(problem(401, "UNAUTHORIZED"))
        with self.assertRaises(AuthenticationError) as ctx:
            client.scrape("https://t.me/s/infalt")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ctx.exception.status, 401)

    def test_400_raises_validation_error_with_fields_without_retry(self):
        client, transport, _ = make_client(
            problem(400, "VALIDATION_ERROR", "invalid request", pointers=["/location"]))
        with self.assertRaises(ValidationError) as ctx:
            client.search("q")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ctx.exception.title, "VALIDATION_ERROR")
        self.assertEqual(ctx.exception.pointers, ["/location"])
        self.assertIn("/location", str(ctx.exception))

    def test_other_4xx_is_not_retried(self):
        client, transport, _ = make_client(problem(404, "NOT_FOUND"))
        with self.assertRaises(WebApiError) as ctx:
            client.scrape("https://example.com")
        self.assertNotIsInstance(ctx.exception, (ValidationError, AuthenticationError))
        self.assertEqual(len(transport.calls), 1)

    def test_429_is_retried_then_succeeds(self):
        client, transport, sleeps = make_client(problem(429, "TOO_MANY_REQUESTS"), ok(request_id="r2"))
        result = client.scrape("https://example.com")
        self.assertEqual(result.request_id, "r2")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [1.5])             # 2**0 + jitter

    def test_persistent_429_raises_rate_limit_error_after_three_attempts(self):
        client, transport, sleeps = make_client(*[problem(429, "TOO_MANY_REQUESTS")] * 3)
        with self.assertRaises(RateLimitError):
            client.scrape("https://example.com")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [1.5, 2.5])        # exponential backoff, no sleep after the last

    def test_5xx_is_retried_up_to_three_attempts(self):
        client, transport, _ = make_client(problem(503, "UNAVAILABLE"), problem(502, "BAD_GATEWAY"),
                                           problem(500, "REQUEST_FAILED_AFTER_MANY_RETRIES"))
        with self.assertRaises(WebApiError) as ctx:
            client.search("q")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.title, "REQUEST_FAILED_AFTER_MANY_RETRIES")

    def test_408_and_transport_timeouts_are_retried(self):
        client, transport, _ = make_client(problem(408, "TIMEOUT"), TimeoutError("timed out"), ok())
        client.scrape("https://example.com")
        self.assertEqual(len(transport.calls), 3)

    def test_timeouts_on_every_attempt_raise(self):
        client, transport, _ = make_client(*[TimeoutError("timed out")] * 3)
        with self.assertRaises(WebApiError) as ctx:
            client.scrape("https://example.com")
        self.assertEqual(ctx.exception.title, "TIMEOUT")
        self.assertEqual(len(transport.calls), 3)

    def test_other_network_errors_are_not_retried(self):
        client, transport, _ = make_client(ConnectionError("connection refused"))
        with self.assertRaises(WebApiError) as ctx:
            client.scrape("https://example.com")
        self.assertEqual(ctx.exception.title, "NETWORK_ERROR")
        self.assertEqual(len(transport.calls), 1)

    def test_key_never_appears_in_errors_logs_or_repr(self):
        client, _, _ = make_client(problem(429, "TOO_MANY_REQUESTS"), problem(401, "UNAUTHORIZED"))
        with self.assertLogs("scrapers.oxylabs", level="DEBUG") as logs:
            with self.assertRaises(AuthenticationError) as ctx:
                client.search("q")
        self.assertNotIn(DUMMY_KEY, str(ctx.exception))
        self.assertNotIn(DUMMY_KEY, "\n".join(logs.output))
        self.assertNotIn(DUMMY_KEY, repr(client))
        self.assertIn("req-err", "\n".join(logs.output))   # request ids are logged


class ApiKeyTests(unittest.TestCase):
    def test_environment_wins_over_dotenv(self):
        with mock.patch.dict(os.environ, {"OXYLABS_WEB_API_KEY": "from-env"}), \
             mock.patch("local_ai.state.read_env", return_value={"OXYLABS_WEB_API_KEY": "from-dotenv"}):
            self.assertEqual(oxylabs.load_api_key(), "from-env")

    def test_falls_back_to_dotenv(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("local_ai.state.read_env", return_value={"OXYLABS_WEB_API_KEY": "from-dotenv"}):
            self.assertEqual(oxylabs.load_api_key(), "from-dotenv")

    def test_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("local_ai.state.read_env", return_value={}):
            with self.assertRaises(MissingApiKeyError):
                WebApiClient()


class UrllibTransportTests(unittest.TestCase):
    def call(self):
        return oxylabs.urllib_transport("POST", "https://webapi.oxylabs.io/v1/search", {}, b"{}", 1)

    def test_http_errors_are_returned_not_raised(self):
        error = urllib.error.HTTPError("https://x", 429, "Too Many Requests",
                                       email.message.Message(), io.BytesIO(b'{"title": "T"}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            resp = self.call()
        self.assertEqual((resp.status, resp.body), (429, b'{"title": "T"}'))

    def test_timeouts_become_timeout_error(self):
        for exc in (urllib.error.URLError(socket.timeout("timed out")), socket.timeout("timed out")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(TimeoutError):
                    self.call()

    def test_other_failures_become_connection_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns failure")):
            with self.assertRaises(ConnectionError):
                self.call()


if __name__ == "__main__":
    unittest.main()
