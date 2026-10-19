"""Email providers of the `email` notice channel (engine/email_senders.py): the choice by LOOP_EMAIL_PROVIDER, the variables each
needs and the HTTP provider against a local fake server. No network beyond 127.0.0.1, no real SMTP."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import email_senders  # noqa: E402
import notify  # noqa: E402

VARIABLES = ("LOOP_EMAIL_PROVIDER", "LOOP_NOTIFY_TO", "LOOP_SMTP_HOST", "LOOP_SMTP_FROM", "LOOP_EMAIL_HTTP_URL", "LOOP_EMAIL_HTTP_TOKEN",
             "LOOP_EMAIL_HTTP_FROM", "LOOP_ENV_FILE")


class FakeMailService(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeMailService.received.append({"auth": self.headers.get("Authorization"), "json": json.loads(body),
                                         "path": self.path, "api_token": self.headers.get("X-Api-Token")})
        self.send_response(FakeMailService.status)
        self.end_headers()
        self.wfile.write(b"answer")

    def log_message(self, *args):
        pass


class TestEmailSenders(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in VARIABLES}
        for name in VARIABLES:
            os.environ.pop(name, None)
        os.environ["LOOP_ENV_FILE"] = os.path.join(engine_env.ENGINE_TESTS_DIR, "no-such.env")
        FakeMailService.received, FakeMailService.status = [], 200

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_smtp_stays_the_default_and_needs_the_same_variables_as_before(self):
        self.assertEqual(notify.missing_config("email"), ["LOOP_NOTIFY_TO", "LOOP_SMTP_HOST", "LOOP_SMTP_FROM"])
        self.assertIsInstance(email_senders.build_email_sender(), email_senders.SmtpEmailSender)

    def test_http_provider_needs_only_its_url_and_the_recipients(self):
        os.environ["LOOP_EMAIL_PROVIDER"] = "http"
        self.assertEqual(notify.missing_config("email"), ["LOOP_NOTIFY_TO", "LOOP_EMAIL_HTTP_URL"])

    def test_unknown_provider_is_reported_as_not_configured(self):
        os.environ["LOOP_EMAIL_PROVIDER"] = "pigeon"
        self.assertIsNone(email_senders.build_email_sender())
        self.assertTrue(notify.missing_config("email"))

    def test_http_provider_posts_json_with_the_token(self):
        server = HTTPServer(("127.0.0.1", 0), FakeMailService)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        os.environ.update(LOOP_EMAIL_PROVIDER="http", LOOP_NOTIFY_TO="a@x.com,b@x.com", LOOP_EMAIL_HTTP_TOKEN="tok",
                          LOOP_EMAIL_HTTP_URL="http://127.0.0.1:%d/send" % server.server_port)
        ok, detail = notify.channel_email("subject", "<p>hi</p>", {}, "")
        self.assertTrue(ok, detail)
        received = FakeMailService.received[0]
        self.assertEqual((received["auth"], received["json"]), ("Bearer tok", {"to": ["a@x.com", "b@x.com"], "subject": "subject", "html": "<p>hi</p>"}))

    def test_http_error_answer_is_a_failure_without_retry_and_hides_the_token(self):
        server = HTTPServer(("127.0.0.1", 0), FakeMailService)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        FakeMailService.status = 500
        os.environ.update(LOOP_EMAIL_PROVIDER="http", LOOP_NOTIFY_TO="a@x.com", LOOP_EMAIL_HTTP_URL="http://127.0.0.1:%d/send" % server.server_port)
        ok, detail = notify.channel_email("s", "b", {}, "")
        self.assertFalse(ok)
        self.assertIn("HTTP 500", detail)
        self.assertEqual(len(FakeMailService.received), 1)


if __name__ == "__main__":
    unittest.main()
