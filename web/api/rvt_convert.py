"""Vercel Python Function: Design Automation result -> takedown DXF draft.

Called by /api/rvt once the TakedownExport workitem succeeds. Downloads the result zip from the
app's own OSS bucket (only `results/` keys), builds the S-* layer DXF from the Revit elements,
inspects it like a normal upload and stores it in Blob so the review page can continue.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ENGINE_DIR = str(Path(__file__).parent / "_engine")
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import aps_client  # noqa: E402
import revit_takedown_dxf  # noqa: E402
from blob_utils import blob_put  # noqa: E402
from inspection_utils import inspect_dxf_bytes  # noqa: E402

RESULT_KEY = re.compile(r"^results/[A-Za-z0-9._/-]+\.zip$")


def convert_result(result_key: str, levels: list[str], filename: str | None) -> dict:
    if not RESULT_KEY.match(result_key) or ".." in result_key:
        raise ValueError("Invalid result key")
    client = aps_client.ApsClient()
    bucket = aps_client.default_bucket_key(client.client_id)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = client.download_object(bucket, result_key, Path(tmp) / "result.zip")
        with zipfile.ZipFile(zip_path) as zf:
            name = next(n for n in zf.namelist() if n.endswith("takedown.json"))
            data = json.loads(zf.read(name))
        dxf_path = Path(tmp) / "revit_takedown.dxf"
        report = revit_takedown_dxf.convert(data, dxf_path, levels or None)
        payload = dxf_path.read_bytes()

    stem = Path(filename or "revit").stem
    draft = inspect_dxf_bytes(payload, f"{stem} - revit.dxf")
    draft["suggestions"] = {**draft["suggestions"], **revit_takedown_dxf.APP_LAYER_MAPPING}
    draft["suggestion_source"] = "revit"
    draft["revit_report"] = {
        "links": report["links"],
        "warnings": report["exporter_warnings"],
        "levels": [
            {k: level[k] for k in ("level", "slab_area_sf", "openings", "columns", "column_sizes",
                                    "shear_walls_by_confidence", "cores", "warnings")}
            for level in report["levels"]
        ],
    }
    blob = blob_put(f"drafts/{draft['id']}.dxf", payload, access="private",
                    content_type="application/octet-stream", add_random_suffix=False)
    draft["blob_url"] = blob["url"]
    return draft


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            draft = convert_result(body["result_key"], body.get("levels") or [], body.get("filename"))
            self._json(200, draft)
        except (KeyError, ValueError) as exc:
            self._json(400, {"detail": str(exc)})
        except Exception as exc:
            self._json(500, {"detail": str(exc)})

    def _json(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        pass
