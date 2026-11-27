import json
import unittest
from unittest.mock import patch, MagicMock
import gateway

class GatewayTest(unittest.TestCase):
    def test_rewrites_model_and_bounds_output(self):
        value = json.loads(gateway.prepare(b'{"input":[],"model":"other","max_output_tokens":99999,"include":["x"]}'))
        self.assertEqual(value["model"], gateway.MODEL)
        self.assertEqual(value["max_output_tokens"], gateway.MAX_OUTPUT_TOKENS)
        self.assertTrue(value["stream"])
        self.assertFalse(value["store"])
        self.assertNotIn("include", value)

    def test_origin_requires_configured_modal_https_origin(self):
        for value in ("http://app.modal.run","https://app.modal.run/path",
                      "https://app.modal.run.example.com","https://lo@host.modal.run",
                      "https://app.modal.run?x=1","https://app.modal.run:9999"):
            with self.subTest(value=value), self.assertRaises(gateway.GatewayError):
                gateway.upstream_host(value)

    def test_only_allowed_headers_and_fixed_route(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream; charset=utf-8"
        response.read1.side_effect = [b"data: event\n\n", b""]
        with patch.object(gateway, "HTTPSConnection", return_value=conn):
            self.assertEqual(list(gateway.stream("https://app.modal.run",
                {"Modal-Key":"key", "Modal-Secret":"secret", "Authorization":"untrusted"},
                b'{"input":[]}')), [b"data: event\n\n"])
        self.assertEqual(conn.request.call_args.args, ("POST", "/v1/responses"))
        self.assertNotIn("Authorization", conn.request.call_args.kwargs["headers"])
        conn.close.assert_called_once()

    def test_rejects_redirect_without_reading_body(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 307
        with patch.object(gateway, "HTTPSConnection", return_value=conn):
            with self.assertRaisesRegex(gateway.GatewayError, "HTTP 307"):
                list(gateway.stream("https://app.modal.run",
                    {"Modal-Key":"key","Modal-Secret":"secret"}, b'{"input":[]}'))
        response.read1.assert_not_called()

    def test_oversized_response_fails_closed(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.return_value = b"abcd"
        with patch.object(gateway, "HTTPSConnection", return_value=conn), patch.object(gateway, "MAX_RESPONSE_BYTES", 3):
            with self.assertRaisesRegex(gateway.GatewayError, "byte limit"):
                list(gateway.stream("https://app.modal.run",
                    {"Modal-Key":"key","Modal-Secret":"secret"}, b'{"input":[]}'))
