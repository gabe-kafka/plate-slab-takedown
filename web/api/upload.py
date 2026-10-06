"""Vercel Python Function: DXF upload and inspection.

Handles both file uploads (multipart/form-data) and demo requests (JSON).
Parses DXF layers, uploads to Vercel Blob, returns draft metadata.
"""

from __future__ import annotations

import cgi
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Add engine directory to Python path
ENGINE_DIR = str(Path(__file__).parent / "_engine")
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

from inspection_utils import inspect_dxf_bytes  # noqa: E402
from blob_utils import blob_put  # noqa: E402

DEMO_DIR = Path(__file__).parent / "_engine" / "demo"
DEMO_FILES = {
    "default": {
        "path": DEMO_DIR / "INPUT.dxf",
        "filename": "358 Flatbush - input.dxf",
    },
    "geom_clean_1": {
        "path": DEMO_DIR / "geom_clean_1.dxf",
        "filename": "1025 Atlantic - input.dxf",
    },
    "fulton_356": {
        "path": DEMO_DIR / "356_fulton.dxf",
        "filename": "356 Fulton - input.dxf",
    },
    "franklin_246": {
        "path": DEMO_DIR / "246_franklin.dxf",
        "filename": "246 Franklin - input.dxf",
    },
    "manhattan_1300": {
        "path": DEMO_DIR / "1300_manhattan.dxf",
        "filename": "1300 Manhattan - input.dxf",
    },
}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")

        try:
            if "multipart/form-data" in content_type:
                payload, filename = self._parse_multipart()
            elif "application/json" in content_type:
                payload, filename = self._parse_json_demo()
            else:
                self._error(400, "Expected multipart/form-data or application/json")
                return

            if not filename.lower().endswith(".dxf"):
                self._error(400, "Upload a .dxf file.")
                return

            # Parse DXF and extract layer metadata
            draft = inspect_dxf_bytes(payload, filename)

            # Upload DXF to Vercel Blob for later retrieval during processing
            blob_result = blob_put(
                f"drafts/{draft['id']}.dxf",
                payload,
                access="private",
                content_type="application/octet-stream",
                add_random_suffix=False,
            )
            draft["blob_url"] = blob_result["url"]

            self._json_response(200, draft)

        except Exception as e:
            self._error(500, str(e))

    def _parse_multipart(self) -> tuple[bytes, str]:
        """Parse multipart/form-data and extract the uploaded file."""
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
        }
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ=environ,
        )
        file_item = form["upload"]
        if isinstance(file_item, list):
            file_item = file_item[0]
        payload = file_item.file.read()
        filename = file_item.filename or "upload.dxf"
        return payload, filename

    def _parse_json_demo(self) -> tuple[bytes, str]:
        """Handle demo request: read bundled demo DXF."""
        query = parse_qs(urlparse(self.path).query)
        query_demo_id = query.get("demo_id", [None])[0]

        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            raw = self.rfile.read(content_length)
            # Vercel runtime may return str or bytes
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if raw:
                body = json.loads(raw)

        if not body.get("demo") and query_demo_id is None:
            raise ValueError("JSON body must contain {\"demo\": true}")

        demo_id = str(query_demo_id or body.get("demo_id") or "default")
        demo = DEMO_FILES.get(demo_id)
        if demo is None:
            choices = ", ".join(sorted(DEMO_FILES))
            raise ValueError(f"Unknown demo_id '{demo_id}'. Available demos: {choices}")

        demo_path = demo["path"]
        if not demo_path.exists():
            raise FileNotFoundError(f"Demo DXF is not available: {demo_id}")

        return demo_path.read_bytes(), demo["filename"]

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json_response(status, {"detail": message})

    def log_message(self, format, *args):
        # Suppress default stderr logging
        pass
