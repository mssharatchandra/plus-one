"""The Bolna tool can propose one reminder without saving or exposing the API."""
import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plus_one.engine import Engine
from plus_one.server import ServerConfig, make_server


class BolnaToolTests(unittest.TestCase):
    def test_disabled_auth_preview_and_no_write(self):
        with TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp) / "demo.sqlite3")
            try:
                for secret in ("", "b" * 32):
                    server = make_server(engine, ServerConfig("a" * 32, bolna_tool_secret=secret), port=0)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        def post(headers, text):
                            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                            conn.request("POST", "/tools/bolna/reminder-preview",
                                         json.dumps({"text": text}), headers={"Content-Type": "application/json", **headers})
                            response = conn.getresponse()
                            value = response.status, json.loads(response.read())
                            conn.close()
                            return value

                        if not secret:
                            self.assertEqual(post({}, "remind me to stretch tomorrow at 9am")[0], 404)
                            continue
                        self.assertEqual(post({}, "remind me to stretch tomorrow at 9am")[0], 401)
                        self.assertEqual(post({"Authorization": "Bearer wrong"}, "remind me to stretch tomorrow at 9am")[0], 401)
                        status, proposal = post({"Authorization": "Bearer " + secret},
                                                "remind me to stretch tomorrow at 9am")
                        self.assertEqual(status, 200)
                        self.assertEqual((proposal["status"], proposal["title"], proposal["saved"]),
                                         ("proposal", "stretch", False))
                        status, clarify = post({"Authorization": "Bearer " + secret},
                                               "remind me to stretch")
                        self.assertEqual((status, clarify["status"], clarify["saved"]),
                                         (200, "clarify", False))
                        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
                    finally:
                        server.shutdown(); server.server_close(); thread.join()
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
