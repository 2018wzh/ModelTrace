from __future__ import annotations

import copy
import json
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app as web
import enrollment


TEXT = ", ".join(str(number) for number in range(1, 101))
MODEL = "gpt-5.6-luna"
RAW_USAGE = {"input_tokens": 10, "output_tokens": 150,
             "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 160}
USAGE = {"input_tokens": 10, "output_tokens": 150,
         "reasoning_tokens": 30, "total_tokens": 160}
CHALLENGES = [{"prompt": f"numbers {index}", "expected_count": 100} for index in range(6)]


def completed_response(status="completed"):
    return {"id": "resp_local", "object": "response", "status": status, "model": MODEL,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": TEXT}]}],
            "usage": copy.deepcopy(RAW_USAGE)}


def chat_response(reasoning=None, finish_reason="stop"):
    usage = {"prompt_tokens": 10, "completion_tokens": 150, "total_tokens": 160}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"model": MODEL, "choices": [{"message": {"content": TEXT},
                                          "finish_reason": finish_reason}], "usage": usage}


def sse(events):
    return "".join(f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
                   for kind, payload in events)


class LocalUpstream:
    """Finite local replay; every recorded request receives one supplied reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls.append({"path": self.path, "body": body})
                if fixture.replies:
                    status, content_type, payload = fixture.replies.pop(0)
                else:
                    status, content_type, payload = 500, "application/json", {"error": "Replay exhausted"}
                data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def json_reply(payload, status=200):
    return status, "application/json", payload


def stream_reply(events):
    return 200, "text/event-stream", sse(events)


class CompletionTransportTests(unittest.TestCase):
    def request(self, upstream, api_format="responses", **kwargs):
        return enrollment.request_completion(upstream.url, "local-secret", MODEL, "digits", None,
                                             api_format=api_format, **kwargs)

    def test_chat_missing_reasoning_is_not_explicit_zero(self):
        with LocalUpstream([json_reply(chat_response()), json_reply(chat_response(0))]) as upstream:
            missing = self.request(upstream, "openai", reasoning_effort="high")
            zero = self.request(upstream, "openai")
        self.assertEqual(missing["text"], TEXT)
        self.assertIsNone(missing["usage"]["reasoning_tokens"])
        self.assertEqual(zero["usage"]["reasoning_tokens"], 0)
        self.assertEqual(missing["response_model"], MODEL)
        self.assertEqual(missing["api_format"], "openai")
        self.assertEqual(upstream.calls[0]["body"]["reasoning_effort"], "high")
        self.assertEqual(upstream.calls[0]["path"], "/v1/chat/completions")

    def test_absent_usage_stays_null(self):
        payload = chat_response()
        del payload["usage"]
        with LocalUpstream([json_reply(payload)]) as upstream:
            result = self.request(upstream, "openai")
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["raw_usage"])

    def test_responses_json_body_url_and_terminal_usage(self):
        with LocalUpstream([json_reply(completed_response())]) as upstream:
            result = enrollment.request_completion(
                upstream.url + "/v1/responses", "local-secret", MODEL, "digits", None,
                api_format="responses", system_prompt="system text", reasoning_effort="high")
        self.assertEqual(result["text"], TEXT)
        self.assertEqual(result["usage"], USAGE)
        self.assertEqual(result["raw_usage"], RAW_USAGE)
        self.assertEqual(upstream.calls[0]["path"], "/v1/responses")
        body = upstream.calls[0]["body"]
        self.assertEqual(body["input"], [{"role": "user", "content": [{"type": "input_text", "text": "digits"}]}])
        self.assertEqual(body["instructions"], "system text")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertIs(body["stream"], True)
        self.assertIs(body["store"], False)
        self.assertNotIn("messages", body)

    def test_responses_stream_reads_final_response_not_deltas(self):
        events = [("response.output_text.delta", {"type": "response.output_text.delta", "delta": "wrong"}),
                  ("response.completed", {"type": "response.completed", "response": completed_response()})]
        reply = (200, "text/event-stream", sse(events) + "data: [DONE]\n\n")
        with LocalUpstream([reply]) as upstream:
            result = self.request(upstream)
        self.assertEqual(result["text"], TEXT)
        self.assertEqual(result["usage"], USAGE)
        self.assertEqual(result["api_format"], "responses")

    def test_responses_reject_failed_incomplete_error_and_missing_terminal(self):
        cases = [
            [("response.failed", {"type": "response.failed", "response": {"status": "failed"}})],
            [("response.incomplete", {"type": "response.incomplete", "response": completed_response("incomplete")})],
            [("error", {"type": "error", "message": "failed"})],
            [("response.output_text.delta", {"type": "response.output_text.delta", "delta": TEXT})],
            [("response.completed", {"type": "response.completed", "response": completed_response("incomplete")})],
        ]
        for events in cases:
            with self.subTest(events=events[0][0]):
                with LocalUpstream([stream_reply(events)]) as upstream:
                    with self.assertRaises(RuntimeError):
                        self.request(upstream)
                self.assertEqual(len(upstream.calls), 1)

    def test_done_sentinel_does_not_replace_completed_terminal(self):
        with LocalUpstream([(200, "text/event-stream", "data: [DONE]\n\n")]) as upstream:
            with self.assertRaises(RuntimeError):
                self.request(upstream)

    def test_responses_json_requires_completed_status(self):
        for status in ("failed", "incomplete", "in_progress", None):
            with self.subTest(status=status):
                with LocalUpstream([json_reply(completed_response(status))]) as upstream:
                    with self.assertRaises(RuntimeError):
                        self.request(upstream)

    def test_chat_truncation_and_invalid_usage_are_not_completions(self):
        invalid = chat_response(151)
        for payload in (chat_response(30, "length"), chat_response(30, "content_filter"), invalid):
            with self.subTest(payload=payload):
                with LocalUpstream([json_reply(payload)]) as upstream:
                    with self.assertRaises(RuntimeError):
                        self.request(upstream, "openai")

    def test_anthropic_normalizes_known_counts_only(self):
        payload = {"model": MODEL, "content": [{"type": "text", "text": TEXT}],
                   "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 150}}
        with LocalUpstream([json_reply(payload)]) as upstream:
            result = self.request(upstream, "anthropic", system_prompt="system")
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["usage"]["output_tokens"], 150)
        self.assertIsNone(result["usage"]["reasoning_tokens"])
        self.assertEqual(upstream.calls[0]["path"], "/v1/messages")
        self.assertEqual(upstream.calls[0]["body"]["system"], "system")

    def test_auto_falls_back_to_responses_and_does_not_drop_effort(self):
        unavailable = json_reply({"error": "unsupported route"}, 404)
        with LocalUpstream([unavailable, unavailable, json_reply(completed_response())]) as upstream:
            result = self.request(upstream, "auto")
        self.assertEqual(result["api_format"], "responses")
        self.assertEqual([call["path"] for call in upstream.calls],
                         ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
        with LocalUpstream([unavailable, json_reply(completed_response())]) as upstream:
            self.request(upstream, "auto", reasoning_effort="high")
        self.assertEqual([call["path"] for call in upstream.calls],
                         ["/v1/chat/completions", "/v1/responses"])
        self.assertEqual(upstream.calls[-1]["body"]["reasoning"], {"effort": "high"})

    def test_retryable_http_error_retains_retry_behavior(self):
        with LocalUpstream([json_reply({"error": "busy"}, 503), json_reply(completed_response())]) as upstream:
            with patch.object(enrollment.time, "sleep"):
                result = self.request(upstream)
        self.assertEqual(result["usage"], USAGE)
        self.assertEqual(len(upstream.calls), 2)

    def test_error_message_redacts_api_key(self):
        with LocalUpstream([json_reply({"error": {"message": "bad local-secret"}}, 401)]) as upstream:
            with self.assertRaises(RuntimeError) as caught:
                self.request(upstream)
        self.assertNotIn("local-secret", str(caught.exception))


class ApiEffortTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        profiles = [
            {"effort": effort, "samples": 3, "minimum": math.log1p(index * 100),
             "median": math.log1p(index * 100 + 25), "maximum": math.log1p(index * 100 + 50)}
            for index, effort in enumerate(("low", "medium", "high", "xhigh", "max"))
        ]
        reference = {"schema_version": 1, "feature": "log1p_reasoning_tokens_per_100_numbers",
                     "minimum_outputs": 3, "models": [{"id": MODEL, "reference_count": 15, "profiles": profiles}]}
        reference_patch = patch("effort.load_effort_reference", return_value=reference)
        reference_patch.start()
        self.addCleanup(reference_patch.stop)

    def payload(self, upstream, **extra):
        return {"base_url": upstream.url, "api_key": "local-secret", "api_model": MODEL,
                "api_format": "responses", "reasoning_effort": "high", **extra}

    def test_direct_auto_carries_actual_usage_and_infers_independently(self):
        with LocalUpstream([json_reply(completed_response()) for _ in range(3)]) as upstream:
            with patch.object(enrollment, "generate_challenges", return_value=CHALLENGES):
                response = self.client.post("/api/test/auto", json=self.payload(upstream))
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["api_test"]["received"], 3)
        self.assertEqual(len(result["outputs"]), 3)
        self.assertEqual(result["outputs"][0]["usage"], USAGE)
        self.assertEqual(result["effort"]["model"], MODEL)
        self.assertEqual(result["effort"]["status"], "estimated")
        self.assertEqual(result["effort"]["prediction"], "low")
        self.assertEqual(result["effort"]["observed"]["used_outputs"], 3)
        self.assertEqual(result["effort"]["observed"]["usage_totals"]["reasoning_tokens"], 90)
        self.assertEqual(result["used_outputs"], 3)
        self.assertNotIn("local-secret", response.get_data(as_text=True))

    def test_browser_probe_evidence_reaches_analyze(self):
        outputs = []
        with LocalUpstream([stream_reply([("response.completed", {"type": "response.completed",
                                           "response": completed_response()})]) for _ in range(3)]) as upstream:
            for _ in range(3):
                response = self.client.post("/api/test/probe", json=self.payload(
                    upstream, prompt="digits", expected_count=100))
                self.assertEqual(response.status_code, 200)
                output = response.get_json()
                self.assertTrue(output["accepted"])
                self.assertEqual(output["parsed_numbers"], 100)
                self.assertEqual(output["minimum_numbers"], 80)
                outputs.append(output)
        response = self.client.post("/api/analyze", json={"outputs": outputs, "effort_model": MODEL})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["effort"]["status"], "estimated")
        self.assertEqual(result["effort"]["prediction"], "low")
        self.assertEqual(result["outputs"][0]["usage"], USAGE)
        self.assertEqual(result["effort"]["observed"]["usage_totals"]["output_tokens"], 450)

    def test_manual_without_usage_keeps_identity_result(self):
        response = self.client.post("/api/analyze", json={
            "outputs": [{"text": TEXT, "expected_count": 100} for _ in range(3)], "effort_model": MODEL})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["prediction"])
        self.assertEqual(result["used_outputs"], 3)
        self.assertEqual(result["effort"]["status"], "unavailable")
        self.assertIsNone(result["outputs"][0]["usage"])
        self.assertIsNone(result["effort"]["observed"]["usage_totals"]["reasoning_tokens"])

    def test_auto_uses_exact_request_model_unless_reference_is_explicit(self):
        with LocalUpstream([json_reply(completed_response()) for _ in range(6)]) as upstream:
            with patch.object(enrollment, "generate_challenges", return_value=CHALLENGES):
                alias = self.client.post("/api/test/auto", json=self.payload(
                    upstream, api_model="provider-alias"))
                selected = self.client.post("/api/test/auto", json=self.payload(
                    upstream, api_model="provider-alias", effort_model=MODEL))
        self.assertEqual(alias.status_code, 200)
        self.assertEqual(alias.get_json()["effort"]["status"], "unavailable")
        self.assertEqual(alias.get_json()["effort"]["model"], "provider-alias")
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.get_json()["effort"]["status"], "estimated")
        self.assertEqual(selected.get_json()["effort"]["model"], MODEL)

    def test_analyze_does_not_guess_reference_model(self):
        response = self.client.post("/api/analyze", json={"outputs": [
            {"text": TEXT, "expected_count": 100, "usage": USAGE} for _ in range(3)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["effort"]["status"], "unavailable")
        self.assertIsNone(response.get_json()["effort"]["model"])

    def test_probe_preserves_missing_and_zero_reasoning(self):
        with LocalUpstream([json_reply(chat_response()), json_reply(chat_response(0))]) as upstream:
            missing = self.client.post("/api/test/probe", json=self.payload(
                upstream, api_format="openai", prompt="digits", expected_count=100)).get_json()
            zero = self.client.post("/api/test/probe", json=self.payload(
                upstream, api_format="openai", prompt="digits", expected_count=100)).get_json()
        self.assertIsNone(missing["usage"]["reasoning_tokens"])
        self.assertEqual(zero["usage"]["reasoning_tokens"], 0)

    def test_probe_returns_reported_usage_even_when_number_count_is_rejected(self):
        payload = completed_response()
        payload["output"][0]["content"][0]["text"] = "1, 2, 3"
        with LocalUpstream([json_reply(payload)]) as upstream:
            response = self.client.post("/api/test/probe", json=self.payload(
                upstream, prompt="digits", expected_count=100))
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["parsed_numbers"], 3)
        self.assertEqual(result["usage"], USAGE)

    def test_truncated_responses_never_enter_scoring(self):
        with LocalUpstream([json_reply(completed_response("incomplete")) for _ in range(6)]) as upstream:
            with patch.object(enrollment, "generate_challenges", return_value=CHALLENGES):
                response = self.client.post("/api/test/auto", json=self.payload(upstream))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("prediction", response.get_json())
        self.assertNotIn("effort", response.get_json())
        self.assertEqual(len(upstream.calls), 6)

    def test_invalid_api_options_are_400_before_network(self):
        with LocalUpstream([]) as upstream:
            for route in ("/api/test/probe", "/api/test/auto"):
                for options in ({"api_format": "bad"}, {"reasoning_effort": "invalid"},
                                {"api_format": "anthropic", "reasoning_effort": "high"},
                                {"api_format": []}):
                    with self.subTest(route=route, options=options):
                        response = self.client.post(route, json=self.payload(
                            upstream, prompt="digits", expected_count=100, model_label="local", **options))
                        self.assertEqual(response.status_code, 400)
            self.assertEqual(upstream.calls, [])

    def test_probe_upstream_failure_returns_502(self):
        with LocalUpstream([stream_reply([("response.failed", {"type": "response.failed"})])]) as upstream:
            response = self.client.post("/api/test/probe", json=self.payload(
                upstream, prompt="digits", expected_count=100))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("usage", response.get_json())

    def test_manual_invalid_usage_returns_400(self):
        response = self.client.post("/api/analyze", json={"outputs": [
            {"text": TEXT, "expected_count": 100, "usage": {"reasoning_tokens": -1}}]})
        self.assertEqual(response.status_code, 400)


    def test_enrollment_persists_completion_metadata(self):
        tasks = [{"prompt": "digits", "user_prefix": "", "system": "system",
                  "condition": "clean", "challenge_id": f"local-{index}",
                  "expected_count": 100, "transport": "clean"} for index in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "reference.jsonl"
            with LocalUpstream([json_reply(completed_response()) for _ in range(3)]) as upstream:
                with patch.object(enrollment, "fingerprint_suite", return_value=tasks):
                    with patch.object(enrollment, "rebuild_bank", return_value={"models": []}):
                        result = enrollment.enroll_automatic(
                            upstream.url, "local-secret", MODEL, "local-label", 3, None,
                            api_format="responses", data_file=data_file,
                            bank_file=Path(directory) / "bank.json")
            rows = [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["usage"], USAGE)
            self.assertEqual(row["raw_usage"], RAW_USAGE)
            self.assertEqual(row["response_model"], MODEL)
            self.assertEqual(row["api_model"], MODEL)
            self.assertEqual(row["api_format"], "responses")
            self.assertEqual(row["parsed_count"], 100)
            self.assertNotIn("local-secret", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
