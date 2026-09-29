"""Minimal in-process fake of the APS endpoints used by aps_client (OAuth, OSS direct-to-S3, DA)."""
from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


class FakeAps:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.part_puts: list[int] = []
        self.fail_once: dict[int, int] = {}
        self.fail_always: set[int] = set()
        self.workitems: dict[str, dict] = {}
        self.activities: dict[str, dict] = {}
        self.appbundles: dict[str, dict] = {}
        self.result_payload: bytes | None = None
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _json(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, data: bytes):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                server.handle(self, "POST")

            def do_GET(self):
                server.handle(self, "GET")

            def do_PUT(self):
                server.handle(self, "PUT")

            def do_PATCH(self):
                server.handle(self, "PATCH")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def handle(self, h, method):
        url = urlparse(h.path)
        path, query = url.path, parse_qs(url.query)
        body = h._body()

        if path == "/authentication/v2/token":
            return h._json(200, {"access_token": "fake.jwt.token", "expires_in": 3599})
        if path.startswith("/s3/part/"):
            upload_key, number = path.split("/")[3], int(path.split("/")[4])
            with self.lock:
                self.part_puts.append(number)
                if number in self.fail_always:
                    return h._json(500, {"error": "boom"})
                if self.fail_once.get(number):
                    status = self.fail_once.pop(number)
                    return h._json(status, {"error": "injected"})
                self.uploads[upload_key]["parts"][number] = body
            return h._json(200, {})
        if path.startswith("/s3/object/"):
            return h._bytes(self.objects[unquote(path[len("/s3/object/"):])])

        match = re.match(r"^/oss/v2/buckets/([^/]+)/objects/([^/]+)/(details|signeds3upload|signeds3download)$", path)
        if path == "/oss/v2/buckets" and method == "POST":
            return h._json(200, {"bucketKey": json.loads(body)["bucketKey"]})
        if match:
            bucket, key, action = match.group(1), unquote(match.group(2)), match.group(3)
            full = f"{bucket}/{key}"
            if action == "details":
                if full in self.objects:
                    return h._json(200, {"objectKey": key, "size": len(self.objects[full])})
                return h._json(404, {"reason": "not found"})
            if action == "signeds3download":
                return h._json(200, {"status": "complete", "url": f"{self.base}/s3/object/{full}"})
            if method == "GET":
                upload_key = query.get("uploadKey", [None])[0]
                if upload_key is None:
                    upload_key = f"uk{len(self.uploads)}"
                    self.uploads[upload_key] = {"object": full, "parts": {}}
                first, parts = int(query["firstPart"][0]), int(query["parts"][0])
                urls = [f"{self.base}/s3/part/{upload_key}/{n}" for n in range(first, first + parts)]
                return h._json(200, {"uploadKey": upload_key, "urls": urls})
            payload = json.loads(body)
            upload = self.uploads[payload["uploadKey"]]
            data = b"".join(upload["parts"][n] for n in sorted(upload["parts"]))
            if payload.get("size") is not None and payload["size"] != len(data):
                return h._json(400, {"reason": "size mismatch"})
            self.objects[upload["object"]] = data
            return h._json(200, {"objectKey": key, "size": len(data), "objectId": f"urn:adsk.objects:os.object:{full}"})

        da = "/da/us-east/v3"
        if path == f"{da}/forgeapps/me":
            return h._json(200, "fakenick")
        if path.startswith(f"{da}/engines/"):
            return h._json(200, {"id": unquote(path.split("/")[-1]), "description": "Revit 2026"})
        if path == f"{da}/servicelimits/me":
            return h._json(200, {"frontendLimits": {"limitPayloadSizeInKB": 16}})
        if path == f"{da}/appbundles" and method == "POST":
            spec = json.loads(body)
            self.appbundles[spec["id"]] = spec
            return h._json(200, {"version": 1, "uploadParameters": {"endpointURL": f"{self.base}/s3/form", "formData": {"key": "x"}}})
        if path == "/s3/form":
            return h._json(200, {})
        if re.match(rf"^{da}/(appbundles|activities)/[^/]+/aliases$", path):
            return h._json(200, {})
        if path == f"{da}/activities" and method == "POST":
            spec = json.loads(body)
            self.activities[f"fakenick.{spec['id']}+prod"] = spec
            return h._json(200, {"version": 1})
        if path.startswith(f"{da}/activities/") and method == "GET":
            name = unquote(path.split("/")[-1])
            return h._json(200, self.activities[name]) if name in self.activities else h._json(404, {})
        if path == f"{da}/workitems" and method == "POST":
            payload = json.loads(body)
            wid = f"wi{len(self.workitems)}"
            self.workitems[wid] = {"payload": payload, "polls": 0}
            return h._json(200, {"id": wid, "status": "pending"})
        if path.startswith(f"{da}/workitems/"):
            wid = path.split("/")[-1]
            item = self.workitems[wid]
            item["polls"] += 1
            if item["polls"] < 2:
                return h._json(200, {"id": wid, "status": "inprogress"})
            result_url = item["payload"]["arguments"]["result"]["url"]
            object_id = unquote(result_url.split("os.object:", 1)[1])
            self.objects[object_id] = self.result_payload or b""
            return h._json(200, {"id": wid, "status": "success", "reportUrl": f"{self.base}/report/{wid}"})
        if path.startswith("/report/"):
            return h._bytes(b"fake DA report\n")
        return h._json(404, {"path": path})


def zip_result(takedown: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("takedown.json", json.dumps(takedown))
        zf.writestr("log.txt", "fake")
    return buffer.getvalue()
