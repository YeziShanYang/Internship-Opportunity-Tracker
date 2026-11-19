"""The urllib transport behind Workday hosts must look like httpx to every caller.

Checked against a local server rather than Workday, so the suite needs no network. The
behaviours tested are the ones a caller relies on: an error status is a response, a
redirect is followed by httpx and recorded, a JSON POST arrives intact, and a dead host
raises an httpx exception.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import unittest

import httpx

from core import paths
from gather import clients, urllib_transport


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/redirect":
            self._send(302, headers=[("Location", "/final")])
        elif self.path == "/final":
            self._send(200, b"landed")
        elif self.path == "/throttled":
            self._send(429, b"Security Check", [("Retry-After", "60")])
        elif self.path == "/headers":
            self._send(200, json.dumps(dict(self.headers)).encode(),
                       [("Content-Type", "application/json")])
        else:
            self._send(404, b"missing")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self._send(200, json.dumps({"echo": json.loads(body),
                                    "type": self.headers["Content-Type"]}).encode(),
                   [("Content-Type", "application/json")])


class UrllibTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.client = httpx.Client(
            transport=urllib_transport.UrllibTransport(), follow_redirects=True,
            headers={"User-Agent": paths.USER_AGENT})

    def tearDown(self):
        self.client.close()

    def test_a_json_post_arrives_intact(self):
        response = self.client.post(f"{self.base}/jobs", json={"limit": 20, "offset": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["echo"], {"limit": 20, "offset": 0})
        self.assertEqual(response.json()["type"], "application/json")

    def test_an_error_status_is_a_response_that_raise_for_status_reads(self):
        """The 429/503 retry in gather.collect classifies on the status code."""
        response = self.client.get(f"{self.base}/throttled")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        with self.assertRaises(httpx.HTTPStatusError):
            response.raise_for_status()

    def test_httpx_follows_the_redirect_and_records_where_it_landed(self):
        """process.redirect judges a fetch by its final URL."""
        response = self.client.get(f"{self.base}/redirect")
        self.assertEqual(response.text, "landed")
        self.assertEqual(response.url.path, "/final")
        self.assertEqual(len(response.history), 1)

    def test_the_honest_user_agent_is_sent_and_nothing_browser_like_is_added(self):
        sent = self.client.get(f"{self.base}/headers").json()
        self.assertEqual(sent["User-Agent"], paths.USER_AGENT)
        self.assertNotIn("Cookie", sent)
        self.assertEqual(sent.get("Accept-Encoding", "identity"), "identity",
                         "urllib's own request shape, not httpx's gzip/keep-alive")

    def test_a_dead_host_raises_an_httpx_error(self):
        """Callers catch httpx.HTTPError; a urllib exception would escape them."""
        with socket.socket() as probe:  # a port with nothing listening
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(httpx.ConnectError):
            self.client.get(f"http://127.0.0.1:{port}/")


class MountTests(unittest.TestCase):
    def test_only_workday_hosts_use_the_urllib_transport(self):
        client = clients.build_web_client()
        transport = client._transport_for_url
        self.assertIsInstance(transport(httpx.URL("https://cibc.wd3.myworkdayjobs.com/x")),
                              urllib_transport.UrllibTransport)
        for other in ("https://boards-api.greenhouse.io/v1/boards/x/jobs",
                      "https://myworkdayjobs.com.example.test/",
                      "https://api.lever.co/v0/postings/x"):
            with self.subTest(other):
                self.assertNotIsInstance(transport(httpx.URL(other)),
                                         urllib_transport.UrllibTransport)

    def test_the_github_client_is_untouched(self):
        """The PAT must never travel through anything but the GitHub client."""
        client = clients.build_github_client(None)
        self.assertNotIsInstance(
            client._transport_for_url(httpx.URL("https://cibc.wd3.myworkdayjobs.com/")),
            urllib_transport.UrllibTransport)


if __name__ == "__main__":
    unittest.main()
