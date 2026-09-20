"""Local HTTP server for the Codex + Jev boundary router."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import RouterConfig
from .engine import Decision, RouterEngine
from .policy import estimate_context_tokens, inspect_request
from .state import ThreadStateStore
from .sse import SSEUsageTracker, assemble_sse
from .stream_debug import RawStreamCapture
from .upstream import HOP_BY_HOP, UpstreamClient


def _read_chunked(stream) -> bytes:
    chunks = []
    while True:
        line = stream.readline()
        if not line:
            raise ConnectionError("unexpected EOF in chunked request")
        size = int(line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            while stream.readline() not in (b"\r\n", b"\n", b""):
                pass
            return b"".join(chunks)
        chunk = stream.read(size)
        if len(chunk) != size or stream.read(2) != b"\r\n":
            raise ConnectionError("invalid chunked request body")
        chunks.append(chunk)


def relay_sse_response(response, writer, tracker: SSEUsageTracker, capture: RawStreamCapture) -> bool:
    """Relay SSE chunks, draining upstream for accounting after a client disconnect."""

    downstream_connected = True
    while True:
        try:
            chunk = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
        except Exception as exc:
            capture.event("upstream_error", error=type(exc).__name__)
            raise
        if not chunk:
            capture.event("upstream_eof")
            break
        capture.chunk(chunk)
        tracker.feed(chunk)
        if not downstream_connected:
            continue
        try:
            writer.write(f"{len(chunk):X}\r\n".encode("ascii"))
            writer.write(chunk)
            writer.write(b"\r\n")
            writer.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            capture.event("client_disconnect", error=type(exc).__name__)
            downstream_connected = False
    return downstream_connected


class DecisionLogger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)


def text_preview(text: str, limit: int = 160) -> str:
    text = re.sub(
        r"(?i)(TYPESAFE_API_KEY\s*=\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def task_preview(payload: dict[str, Any], limit: int = 160) -> str:
    return text_preview(inspect_request(payload).task, limit)


@dataclass
class RouterApp:
    config: RouterConfig
    store: ThreadStateStore
    engine: RouterEngine
    upstream: UpstreamClient
    logger: DecisionLogger
    jev_key_loaded: bool


def build_app(config: RouterConfig, *, key_loader=None, jev_factory=None) -> RouterApp:
    store = ThreadStateStore(config.state_path)
    engine_kwargs = {}
    if key_loader is not None:
        engine_kwargs["key_loader"] = key_loader
    if jev_factory is not None:
        engine_kwargs["jev_factory"] = jev_factory
    engine = RouterEngine(config, store, **engine_kwargs)
    if config.upstream_mode == "caller_edge":
        with open(config.caller_secret_path, encoding="utf-8") as handle:
            secret = handle.read().strip()
        upstream = UpstreamClient.caller_edge(
            config.caller_edge_url,
            secret,
            config.upstream_timeout_seconds,
        )
    else:
        upstream = UpstreamClient.direct(config.direct_url, config.upstream_timeout_seconds)
    try:
        jev_key_loaded = bool(engine.key_loader())
    except Exception:
        jev_key_loaded = False
    return RouterApp(
        config,
        store,
        engine,
        upstream,
        DecisionLogger(config.decision_log_path),
        jev_key_loaded,
    )


class RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, app: RouterApp):
        self.app = app
        super().__init__(address, handler)

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"codex-jev-router/{__version__}"

    def log_message(self, *_args):
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._headers_sent = True
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/health"):
            return self._json(
                200,
                {
                    "ok": True,
                    "service": "codex-jev-router",
                    "version": __version__,
                    "jev_key": self.server.app.jev_key_loaded,
                },
            )
        if path in ("/models", "/v1/models"):
            return self._json(
                200,
                {
                    "object": "list",
                    "models": [],
                    "data": [
                        {
                            "id": "auto",
                            "object": "model",
                            "created": 1789747200,
                            "owned_by": "jev",
                            "name": "Codex + Jev Router",
                        }
                    ],
                },
            )
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        self._headers_sent = False
        self._active_capture = None
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except Exception as exc:
            if self._headers_sent:
                self._capture_event("handler_error", error=type(exc).__name__)
                self.close_connection = True
                return
            try:
                self._json(502, {"error": {"message": f"codex-jev-router: {type(exc).__name__}"}})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _capture_event(self, name: str, **fields: Any) -> None:
        capture = getattr(self, "_active_capture", None)
        if capture is None:
            return
        try:
            capture.event(name, **fields)
        except OSError as exc:
            print(
                f"[codex-jev-router] capture event failed: {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return _read_chunked(self.rfile)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ConnectionError("incomplete request body")
        return body

    def _post(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/responses", "/v1/responses"):
            return self._json(404, {"error": {"message": "unsupported path"}})
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": {"message": "invalid JSON"}})
        if not isinstance(payload, dict):
            return self._json(400, {"error": {"message": "JSON object expected"}})

        started = time.monotonic()
        request_started_monotonic_ns = time.monotonic_ns()
        request_id = str(uuid.uuid4())
        request_headers = list(self.headers.raw_items())
        body_chars = len(raw.decode("utf-8", "replace"))
        decision = self.server.app.engine.decide(dict(request_headers), payload, body_chars)
        stream_requested = payload.get("stream") is True
        payload["model"] = decision.model
        reasoning = payload.get("reasoning")
        reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
        reasoning["effort"] = decision.effort
        payload["reasoning"] = reasoning
        payload["service_tier"] = decision.service_tier
        payload["stream"] = True

        connection = None
        status = 0
        out_kind = ""
        upstream_content_type = ""
        tracker = SSEUsageTracker(
            on_spawn=lambda delegation: self.server.app.engine.record_delegations(
                decision.identity.thread_id,
                [delegation],
            )
        )
        capture = RawStreamCapture.start(
            sentinel=self.server.app.config.stream_debug_path,
            directory=self.server.app.config.raw_stream_dir,
            request_id=request_id,
            thread_id=decision.identity.thread_id,
        )
        self._active_capture = capture
        recorded = False

        def finish_record() -> None:
            nonlocal recorded
            if recorded:
                return
            tracker.finish()
            usage = tracker.usage
            if isinstance(usage.input_tokens, int):
                context_tokens = usage.input_tokens
                context_source = "usage"
            else:
                context_tokens = estimate_context_tokens(
                    body_chars,
                    self.server.app.config.chars_per_token,
                )
                context_source = "estimate"
            try:
                self.server.app.engine.record_usage(decision.identity.thread_id, context_tokens)
            except OSError as exc:
                self._capture_event("record_error", stage="state", error=type(exc).__name__)
                print(
                    f"[codex-jev-router] state record failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            record = {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "thread_id": decision.identity.thread_id,
                    "turn_id": decision.identity.turn_id,
                    "parent_thread_id": decision.identity.parent_thread_id,
                    "parent_tier": decision.parent_tier,
                    "agent_name": decision.agent_name,
                    "subagent_kind": decision.subagent_kind,
                    "delegation_source": decision.delegation_source,
                    "is_subagent": decision.identity.is_subagent,
                    "event": decision.event,
                    "gate": decision.gate,
                    "reason": decision.reason,
                    "consulted_jev": decision.consulted_jev,
                    "jev_ms": decision.jev_ms,
                    "jev": decision.jev,
                    "shadow": decision.shadow,
                    "model": decision.model,
                    "effort": decision.effort,
                    "service_tier": decision.service_tier,
                    "context_tokens": decision.context_tokens,
                    "context_source": context_source,
                    "cost": asdict(decision.cost) if decision.cost else None,
                    "would": decision.would,
                    "status": status,
                    "out": out_kind,
                    "upstream_content_type": upstream_content_type,
                    "upstream_model": tracker.response_model,
                    "response_completed": tracker.response_completed,
                    "usage": asdict(usage),
                    "task": text_preview(decision.routing_task),
                    "routing_task": text_preview(decision.routing_task),
                    "total_ms": int((time.monotonic() - started) * 1000),
                    "request_started_monotonic_ns": request_started_monotonic_ns,
                    "response_finished_monotonic_ns": time.monotonic_ns(),
                    "stream_capture": capture.relative_name,
                }
            try:
                self.server.app.logger.append(record)
            except OSError as exc:
                self._capture_event("record_error", stage="decision_log", error=type(exc).__name__)
                print(
                    f"[codex-jev-router] decision log failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            recorded = True

        try:
            connection, response = self.server.app.upstream.open_response(payload, request_headers, path)
            status = response.status
            upstream_content_type = (response.getheader("Content-Type") or "").strip()
            is_sse = "text/event-stream" in upstream_content_type or status == 200
            if is_sse and stream_requested:
                out_kind = "sse"
                self.send_response(status)
                for name, value in response.getheaders():
                    if name.lower() in HOP_BY_HOP or name.lower() in ("content-length", "content-type"):
                        continue
                    self.send_header(name, value)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._headers_sent = True
                downstream_connected = relay_sse_response(response, self.wfile, tracker, capture)
                finish_record()
                if downstream_connected:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError) as exc:
                        capture.event("client_disconnect", error=type(exc).__name__)
            else:
                out_kind = "json"
                data = response.read()
                capture.chunk(data)
                capture.event("upstream_eof")
                tracker.feed(data)
                output_content_type = upstream_content_type or "application/json"
                head = data[:64].lstrip()
                if status == 200 and (head.startswith(b"event:") or head.startswith(b"data:")):
                    assembled = assemble_sse(data)
                    if assembled is not None:
                        data = json.dumps(assembled, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        output_content_type = "application/json"
                finish_record()
                self.send_response(status)
                self.send_header("Content-Type", output_content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self._headers_sent = True
                self.wfile.write(data)
        finally:
            if connection is not None:
                connection.close()
            finish_record()
            try:
                capture.close(response_completed=tracker.response_completed)
            except OSError as exc:
                print(
                    f"[codex-jev-router] stream capture close failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )


def make_server(address, app: RouterApp) -> RouterHTTPServer:
    return RouterHTTPServer(address, RouterHandler, app)


def serve(config: RouterConfig) -> None:
    app = build_app(config)
    server = make_server((config.listen_host, config.listen_port), app)
    print(
        f"[codex-jev-router] listening http://{config.listen_host}:{server.server_port} "
        f"upstream={config.upstream_mode}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
