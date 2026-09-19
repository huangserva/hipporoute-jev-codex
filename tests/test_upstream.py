import unittest
from unittest import mock
from urllib.parse import urlsplit

from codex_jev_router.upstream import UpstreamClient, make_connection


class UpstreamTests(unittest.TestCase):
    def test_https_direct_mode_honors_https_proxy_with_connect(self):
        upstream = urlsplit("https://chatgpt.com/backend-api/codex")
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7897"}, clear=True):
            with mock.patch("codex_jev_router.upstream.http.client.HTTPSConnection") as connection:
                result = make_connection(upstream, timeout=123)
        connection.assert_called_once_with("127.0.0.1", 7897, timeout=123, context=mock.ANY)
        result.set_tunnel.assert_called_once_with("chatgpt.com", 443)

    def test_direct_path_strips_local_v1_prefix(self):
        client = UpstreamClient.direct("https://chatgpt.com/backend-api/codex", timeout=10)
        self.assertEqual(client.request_path("/v1/responses"), "/backend-api/codex/responses")

    def test_caller_edge_path_includes_secret(self):
        client = UpstreamClient.caller_edge("http://127.0.0.1:4202", "local-secret", timeout=10)
        self.assertEqual(
            client.request_path("/v1/responses"),
            "/_codex-router/local-secret/v1/responses",
        )


if __name__ == "__main__":
    unittest.main()
