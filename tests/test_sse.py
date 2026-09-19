import json
import unittest

from codex_jev_router.sse import SSEUsageTracker, assemble_sse


def event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


class SSETests(unittest.TestCase):
    def test_assemble_sse_uses_completed_response_and_fills_output(self):
        raw = b"".join(
            [
                event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"type": "message", "id": "m1", "content": []},
                    },
                ),
                event(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "status": "completed", "output": []},
                    },
                ),
                b"data: [DONE]\n\n",
            ]
        )
        assembled = assemble_sse(raw)
        self.assertEqual(assembled["id"], "resp_1")
        self.assertEqual(assembled["output"][0]["id"], "m1")

    def test_usage_tracker_handles_arbitrary_chunk_boundaries(self):
        raw = event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "usage": {
                        "input_tokens": 12345,
                        "input_tokens_details": {"cached_tokens": 12000},
                    },
                },
            },
        )
        tracker = SSEUsageTracker()
        for index in range(0, len(raw), 7):
            tracker.feed(raw[index : index + 7])
        tracker.finish()
        self.assertEqual(tracker.usage.input_tokens, 12345)
        self.assertEqual(tracker.usage.cached_tokens, 12000)

    def test_failed_sse_is_assembled_as_error(self):
        raw = event("response.failed", {"type": "response.failed", "response": {"error": {"message": "bad"}}})
        result = assemble_sse(raw)
        self.assertEqual(result["error"]["type"], "response.failed")


if __name__ == "__main__":
    unittest.main()
