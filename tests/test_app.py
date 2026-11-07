from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["SEEDANCE_DATA_DIR"] = cls.temp.name
        import app

        cls.app = app
        app.db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.video = Path(cls.temp.name) / "fixture.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=2",
                "-pix_fmt", "yuv420p", str(cls.video),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, path: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers=headers or {},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health(self):
        status, data = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["model"])

    def test_upload_cuts_and_serves_thumbnail(self):
        raw = self.video.read_bytes()
        status, data = self.request(
            "/api/media/upload",
            "POST",
            raw,
            {"Content-Type": "video/mp4", "X-Filename": "fixture.mp4"},
        )
        self.assertEqual(status, 202)
        media_id = data["media"]["id"]
        deadline = time.time() + 15
        item = None
        while time.time() < deadline:
            _, listing = self.request("/api/media")
            item = next((row for row in listing["media"] if row["id"] == media_id), None)
            if item and item.get("shot_count"):
                break
            time.sleep(0.3)
        self.assertIsNotNone(item)
        self.assertGreaterEqual(item["shot_count"], 1)
        _, detail = self.request(f"/api/media/{media_id}")
        shot = detail["media"]["shots"][0]
        self.assertTrue(shot["thumb_path"])
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/media/{media_id}/{shot['thumb_path']}", timeout=10
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(len(response.read()), 100)

    def test_z_analyse_requires_model(self):
        _, listing = self.request("/api/media")
        if not listing["media"]:
            self.skipTest("upload test did not create media")
        media_id = listing["media"][0]["id"]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(f"/api/media/{media_id}/analyze", "POST", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(caught.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
