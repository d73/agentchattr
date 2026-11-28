from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app


class SVGUploadIsolationTests(unittest.TestCase):
    def test_svg_previews_keep_their_image_type_and_sandbox_document_navigation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><title>Preview fixture</title></svg>'
            (root / "preview.svg").write_bytes(svg)
            (root / "preview.png").write_bytes(b"raster fixture")
            api = FastAPI()
            api.add_api_route("/uploads/{filename}", app.serve_upload, methods=["GET"])

            with (
                patch.object(app, "config", {"images": {"upload_dir": directory}}),
                TestClient(api) as client,
            ):
                preview = client.get("/uploads/preview.svg")
                raster = client.get("/uploads/preview.png")

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, svg)
        self.assertEqual(preview.headers["content-type"], "image/svg+xml")
        policy = {directive.strip() for directive in preview.headers["content-security-policy"].split(";")}
        self.assertIn("sandbox", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertEqual(preview.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("content-disposition", preview.headers)
        self.assertEqual(raster.headers["content-type"], "image/png")
        self.assertNotIn("content-security-policy", raster.headers)
