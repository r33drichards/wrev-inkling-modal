import json
import unittest
import time
from unittest.mock import patch, MagicMock
import gateway

class GatewayTest(unittest.TestCase):
    def test_rewrites_model_and_delegates_output_budget(self):
        value = json.loads(gateway.prepare(b'{"input":[],"model":"other","max_output_tokens":99999,"include":["x"]}'))
        self.assertEqual(value["model"], gateway.MODEL)
        self.assertEqual(value["max_output_tokens"], 99999)
        self.assertNotIn("max_output_tokens", json.loads(gateway.prepare(b'{"input":[]}')))
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
                b'{"input":[]}', deadline=time.monotonic() + 3600)), [b"data: event\n\n"])
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
                    {"Modal-Key":"key","Modal-Secret":"secret"}, b'{"input":[]}', deadline=time.monotonic() + 3600))
        response.read1.assert_not_called()

    def test_transfer_has_no_independent_size_or_time_cap(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        data = b"x" * 65536
        response.read1.side_effect = [data] * 129 + [b""]
        payload = json.dumps({"input":["x" * (16 * 1024 * 1024 + 1)]}).encode()
        with patch.object(gateway, "HTTPSConnection", return_value=conn) as connect:
            observed = sum(map(len, gateway.stream("https://app.modal.run",
                {"Modal-Key":"key","Modal-Secret":"secret"}, payload, deadline=time.monotonic() + 550)))
        self.assertGreater(observed, 8 * 1024 * 1024)
        self.assertGreater(connect.call_args.kwargs["timeout"], 500)
        self.assertGreater(len(conn.request.call_args.kwargs["body"]), 16 * 1024 * 1024)

    def test_sse_chunk_boundaries_and_incomplete(self):
        chunks = iter([b'data: {"type":"response.com', b'pleted","response":{"output":[]}}\r\n\r\n'])
        self.assertEqual(gateway.completed_output(chunks), [])
        with self.assertRaises(gateway.GatewayError):
            gateway.completed_output(iter([b'data: {"type":"response.incomplete"}\n\n']))
        with self.assertRaises(gateway.GatewayError):
            gateway.completed_output(iter([b'data: {"type":"response.completed"}']))
