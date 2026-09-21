import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path

from hipporoute.stream_debug import RawStreamCapture


class RawStreamCaptureTests(unittest.TestCase):
    def test_missing_sentinel_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = RawStreamCapture.start(
                sentinel=root / "stream.debug",
                directory=root / "raw",
                request_id="request-1",
                thread_id="thread-1",
            )
            capture.chunk(b"abc")
            capture.event("upstream_eof")

            self.assertFalse(capture.active)
            self.assertFalse((root / "raw").exists())

    def test_enabled_capture_preserves_chunk_boundaries_and_terminal_events(self):
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
            capture.chunk(b"abc\n")
            capture.chunk(b"\x00xyz")
            capture.event("client_disconnect", error="BrokenPipeError")
            capture.event("upstream_eof")
            capture.close(response_completed=True)

            self.assertTrue(capture.active)
            self.assertIsNotNone(capture.relative_name)
            files = list((root / "raw").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            rows = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], [
                "start", "chunk", "chunk", "client_disconnect", "upstream_eof", "end"
            ])
            chunks = [base64.b64decode(row["data_base64"]) for row in rows if row["event"] == "chunk"]
            self.assertEqual(chunks, [b"abc\n", b"\x00xyz"])
            self.assertEqual([row["chunk_index"] for row in rows if row["event"] == "chunk"], [1, 2])
            self.assertTrue(all(isinstance(row["wall_time"], str) for row in rows))
            self.assertTrue(all(isinstance(row["monotonic_ns"], int) for row in rows))
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertNotIn("thread-1", files[0].name)
            self.assertTrue(rows[-1]["response_completed"])


if __name__ == "__main__":
    unittest.main()
