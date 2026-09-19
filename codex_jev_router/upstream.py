"""HTTP upstream construction for direct ChatGPT and Codex Router caller edge modes."""

from __future__ import annotations

import base64
import http.client
import json
import ssl
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit
from urllib.request import getproxies, proxy_bypass


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def make_connection(upstream, timeout=900):
    port = upstream.port or (443 if upstream.scheme == "https" else 80)
    if upstream.scheme == "https":
        proxy_url = None if proxy_bypass(upstream.hostname) else getproxies().get("https")
        if proxy_url:
            proxy = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
            if proxy.scheme != "http" or not proxy.hostname:
                raise ValueError(f"unsupported HTTPS proxy URL: {proxy_url!r}")
            connection = http.client.HTTPSConnection(
                proxy.hostname,
                proxy.port or 80,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
            tunnel_headers = {}
            if proxy.username is not None:
                credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
                encoded = base64.b64encode(credentials.encode()).decode("ascii")
                tunnel_headers["Proxy-Authorization"] = f"Basic {encoded}"
            if tunnel_headers:
                connection.set_tunnel(upstream.hostname, port, headers=tunnel_headers)
            else:
                connection.set_tunnel(upstream.hostname, port)
            return connection
        return http.client.HTTPSConnection(
            upstream.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    if upstream.scheme == "http":
        return http.client.HTTPConnection(upstream.hostname, port, timeout=timeout)
    raise ValueError("upstream URL must use http or https")


def _join(base_path: str, suffix: str) -> str:
    return f"{base_path.rstrip('/')}/{suffix.lstrip('/')}"


@dataclass(frozen=True)
class UpstreamClient:
    mode: str
    base_url: str
    timeout: float
    caller_secret: str | None = None

    @classmethod
    def direct(cls, base_url: str, timeout: float):
        return cls("direct", base_url, timeout)

    @classmethod
    def caller_edge(cls, base_url: str, caller_secret: str, timeout: float):
        return cls("caller_edge", base_url, timeout, caller_secret)

    @property
    def parsed(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid upstream base URL")
        return parsed

    def request_path(self, incoming_path: str) -> str:
        parsed = self.parsed
        path = incoming_path.split("?", 1)[0]
        if self.mode == "direct":
            if path.startswith("/v1/"):
                path = path[3:]
            return _join(parsed.path, path)
        secret = quote(self.caller_secret or "", safe="")
        return _join(parsed.path, f"/_codex-router/{secret}/v1/responses")

    def open_response(
        self,
        payload: dict,
        incoming_headers: Iterable[tuple[str, str]],
        incoming_path: str,
    ):
        parsed = self.parsed
        connection = make_connection(parsed, timeout=self.timeout)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = self.request_path(incoming_path)
        connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        default_port = 443 if parsed.scheme == "https" else 80
        host = parsed.hostname if not parsed.port or parsed.port == default_port else f"{parsed.hostname}:{parsed.port}"
        connection.putheader("Host", host)
        if self.mode == "direct":
            for name, value in incoming_headers:
                lower = name.lower()
                if lower in HOP_BY_HOP or lower in {
                    "host",
                    "content-length",
                    "content-type",
                    "expect",
                    "accept",
                }:
                    continue
                connection.putheader(name, value)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Accept", "text/event-stream")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        return connection, connection.getresponse()
