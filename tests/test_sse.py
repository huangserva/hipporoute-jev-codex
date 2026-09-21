import json
import unittest

from hipporoute.sse import SSEUsageTracker, assemble_sse


def event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


class SSETests(unittest.TestCase):
    def test_tracker_observes_spawn_delegation_before_stream_finishes(self):
        seen = []
        raw = event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {
                            "task_name": "docstring_policy",
                            "message": "Add a docstring to inspect_request.",
                        }
                    ),
                },
            },
        )
        tracker = SSEUsageTracker(on_spawn=seen.append)

        for index in range(0, len(raw), 5):
            tracker.feed(raw[index : index + 5])

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].agent_name, "docstring_policy")
        self.assertEqual(seen[0].task, "Add a docstring to inspect_request.")
        self.assertEqual(tracker.spawn_delegations, seen)

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
                    "model": "gpt-5.6-luna",
                    "usage": {
                        "input_tokens": 12345,
                        "input_tokens_details": {"cached_tokens": 12000},
                        "output_tokens": 321,
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
        self.assertEqual(tracker.usage.output_tokens, 321)
        self.assertEqual(tracker.response_model, "gpt-5.6-luna")
        self.assertTrue(tracker.response_completed)

    def test_tracker_marks_stream_without_completed_event(self):
        tracker = SSEUsageTracker()
        tracker.feed(event("response.output_text.done", {"type": "response.output_text.done"}))
        tracker.finish()
        self.assertFalse(tracker.response_completed)

    def test_failed_sse_is_assembled_as_error(self):
        raw = event("response.failed", {"type": "response.failed", "response": {"error": {"message": "bad"}}})
        result = assemble_sse(raw)
        self.assertEqual(result["error"]["type"], "response.failed")


if __name__ == "__main__":
    unittest.main()
