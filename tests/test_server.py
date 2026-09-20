import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codex_jev_router.config import load_config
from codex_jev_router.server import build_app, make_server
from codex_jev_router.server import relay_sse_response
from codex_jev_router.sse import SSEUsageTracker
from codex_jev_router.stream_debug import RawStreamCapture


OUTPUT_ITEM = {
    "type": "response.output_item.done",
    "output_index": 0,
    "item": {
        "type": "message",
        "id": "m1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "OK"}],
    },
}
COMPLETED = {
    "type": "response.completed",
    "response": {
        "id": "resp_1",
        "model": "gpt-6-astra",
        "status": "completed",
        "output": [],
        "usage": {
            "input_tokens": 4321,
            "input_tokens_details": {"cached_tokens": 4000},
            "output_tokens": 37,
        },
    },
}
SSE_BYTES = (
    b"event: response.output_item.done\n"
    + b"data: "
    + json.dumps(OUTPUT_ITEM, separators=(",", ":")).encode()
    + b"\n\n"
    + b"event: response.completed\n"
    + b"data: "
    + json.dumps(COMPLETED, separators=(",", ":")).encode()
    + b"\n\n"
    + b"data: [DONE]\n\n"
)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.__class__.seen.append((self.path, list(self.headers.raw_items()), json.loads(body)))
        self.send_response(200)
        # Deliberately omit Content-Type, matching the caller edge behavior.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in (SSE_BYTES[:19], SSE_BYTES[19:83], SSE_BYTES[83:]):
            self.wfile.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *_args):
        pass


class ServerTests(unittest.TestCase):
    def setUp(self):
        FakeUpstreamHandler.seen.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = replace(
            load_config(None),
            direct_url=f"http://127.0.0.1:{self.upstream.server_port}/backend-api/codex",
            state_path=root / "threads.json",
            decision_log_path=root / "decisions.jsonl",
            off_path=root / "router.off",
            shadow_path=root / "router.shadow",
            stream_debug_path=root / "stream.debug",
            raw_stream_dir=root / "raw-streams",
        )
        self.app = build_app(config, key_loader=lambda: "")
        self.server = make_server(("127.0.0.1", 0), self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def post(self, stream, content="Say OK", *, thread_id="thread-1", metadata_extra=None, input_items=None):
        metadata = {"thread_id": thread_id, "turn_id": "turn-1", "thread_source": "user"}
        metadata.update(metadata_extra or {})
        body = {
            "model": "auto",
            "stream": stream,
            "input": input_items or [{"type": "message", "role": "user", "content": content}],
            "client_metadata": {"thread_id": thread_id, "turn_id": "turn-1"},
        }
        headers = {
            "Content-Type": "application/json",
            "thread-id": thread_id,
            "x-codex-turn-metadata": json.dumps(metadata),
        }
        if metadata.get("parent_thread_id"):
            headers["x-codex-parent-thread-id"] = metadata["parent_thread_id"]
            headers["x-openai-subagent"] = "collab_spawn"
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("POST", "/v1/responses", json.dumps(body), headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def test_health_and_models_endpoints(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", "/health")
        health = conn.getresponse()
        health_data = json.loads(health.read())
        conn.request("GET", "/v1/models")
        models = conn.getresponse()
        models_data = json.loads(models.read())
        conn.close()
        self.assertTrue(health_data["ok"])
        self.assertIs(health_data["jev_key"], False)
        self.assertEqual(models_data["data"][0]["id"], "auto")
        self.assertEqual(models_data["models"], [])

    def test_direct_upstream_receives_one_content_type_header(self):
        self.post(True)
        raw_headers = FakeUpstreamHandler.seen[-1][1]
        content_types = [value for name, value in raw_headers if name.lower() == "content-type"]
        self.assertEqual(content_types, ["application/json"])

    def test_streaming_response_is_redeclared_and_relayed_byte_exact(self):
        status, headers, data = self.post(True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(data, SSE_BYTES)
        forwarded = FakeUpstreamHandler.seen[-1][2]
        self.assertTrue(forwarded["stream"])
        self.assertEqual(forwarded["model"], "gpt-6-astra")
        self.assertEqual(forwarded["reasoning"]["effort"], "medium")

    def test_nonstream_caller_receives_assembled_json(self):
        status, headers, data = self.post(False)
        result = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(result["id"], "resp_1")
        self.assertEqual(result["output"][0]["id"], "m1")
        self.assertTrue(FakeUpstreamHandler.seen[-1][2]["stream"])

    def test_completed_usage_updates_thread_state_and_log(self):
        self.post(True)
        state = self.app.store.get("thread-1")
        self.assertEqual(state.last_context_tokens, 4321)
        record = json.loads(self.app.config.decision_log_path.read_text().splitlines()[-1])
        self.assertEqual(record["gate"], "no_key")
        self.assertEqual(record["usage"]["cached_tokens"], 4000)
        self.assertEqual(record["usage"]["output_tokens"], 37)
        self.assertEqual(record["upstream_model"], "gpt-6-astra")
        self.assertTrue(record["response_completed"])
        self.assertIsNone(record["jev"])
        self.assertNotIn("authorization", json.dumps(record).lower())

    def test_debug_sentinel_captures_exact_upstream_chunks_and_links_log(self):
        self.app.config.stream_debug_path.touch()

        self.post(True)

        files = list(self.app.config.raw_stream_dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text().splitlines()]
        captured = b"".join(
            __import__("base64").b64decode(row["data_base64"])
            for row in rows
            if row["event"] == "chunk"
        )
        self.assertEqual(captured, SSE_BYTES)
        record = json.loads(self.app.config.decision_log_path.read_text().splitlines()[-1])
        self.assertEqual(record["stream_capture"], files[0].name)

    def test_downstream_disconnect_still_drains_upstream_through_completed(self):
        class Response:
            def __init__(self):
                self.chunks = iter((SSE_BYTES[:83], SSE_BYTES[83:], b""))

            def read1(self, _size):
                return next(self.chunks)

        class DisconnectedWriter:
            def write(self, _data):
                raise BrokenPipeError("client left")

            def flush(self):
                raise AssertionError("flush must not run after failed write")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "stream.debug"
            sentinel.touch()
            capture = RawStreamCapture.start(
                sentinel=sentinel,
                directory=root / "raw",
                request_id="request-1",
                thread_id="thread-1",
            )
            tracker = SSEUsageTracker()

            downstream_connected = relay_sse_response(
                Response(), DisconnectedWriter(), tracker, capture
            )
            tracker.finish()
            capture.close(response_completed=tracker.response_completed)

            self.assertFalse(downstream_connected)
            self.assertTrue(tracker.response_completed)
            self.assertEqual(tracker.usage.input_tokens, 4321)
            rows = [json.loads(line) for line in next((root / "raw").glob("*.jsonl")).read_text().splitlines()]
            events = [row["event"] for row in rows if row["event"] != "chunk"]
            self.assertEqual(events, ["start", "client_disconnect", "upstream_eof", "end"])

    def test_subagent_log_contains_parent_chain_and_routing_source(self):
        self.post(True, "Coordinate a hard task", thread_id="parent")
        child_items = [
            {"type": "message", "role": "user", "content": "Coordinate a hard task"},
            {
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/docstring_policy",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Message Type: NEW_TASK\nTask name: /root/docstring_policy\n"
                            "Sender: /root\nPayload:\n"
                        ),
                    },
                    {"type": "encrypted_content", "encrypted_content": "gAAAAAopaque"},
                ],
            },
        ]
        self.post(
            True,
            thread_id="child",
            metadata_extra={
                "thread_source": "subagent",
                "parent_thread_id": "parent",
                "agent_name": "/root/docstring_policy",
                "subagent_kind": "thread_spawn",
            },
            input_items=child_items,
        )

        record = json.loads(self.app.config.decision_log_path.read_text().splitlines()[-1])
        self.assertEqual(record["parent_thread_id"], "parent")
        self.assertEqual(record["parent_tier"], "gpt-6-astra")
        self.assertEqual(record["agent_name"], "/root/docstring_policy")
        self.assertEqual(record["subagent_kind"], "thread_spawn")
        self.assertEqual(record["delegation_source"], "agent_name_fallback")
        self.assertIn("docstring_policy", record["routing_task"])

    def test_decision_log_task_is_truncated_and_redacted(self):
        self.post(True, "Rename value. TYPESAFE_API_KEY=super-secret " + "x" * 300)
        record = json.loads(self.app.config.decision_log_path.read_text().splitlines()[-1])
        self.assertTrue(record["task"].startswith("Rename value."))
        self.assertNotIn("super-secret", record["task"])
        self.assertLessEqual(len(record["task"]), 160)


if __name__ == "__main__":
    unittest.main()
