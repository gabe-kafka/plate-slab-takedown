"""Autodesk Platform Services client: 2-legged OAuth, OSS direct-to-S3, Design Automation, Model Derivative.

Standard library only so it runs unchanged in the Vercel Python function and the CLI.

Limits worth knowing (APS docs, 2026):
- OSS signeds3upload: <=25 signed part URLs per request, parts >=5 MB except the last, <=10,000 parts,
  URLs valid 1-60 min. No hard object-size cap (trial hubs: 5 GB total storage).
- Design Automation workitem JSON <=16 KB; OSS URN arguments are fetched by DA itself, which
  refreshes the bearer token, so long queues do not expire inputs.
- Model Derivative RVT->DWG exports only views placed on sheets.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

BASE_URL = "https://developer.api.autodesk.com"
DA_URL = f"{BASE_URL}/da/us-east/v3"
MD_URL = f"{BASE_URL}/modelderivative/v2"
OSS_URL = f"{BASE_URL}/oss/v2"

SCOPES = "data:read data:write data:create bucket:create bucket:read code:all viewables:read"
MIN_PART_BYTES = 5 * 1024 * 1024
DEFAULT_PART_BYTES = 64 * 1024 * 1024
MAX_URLS_PER_REQUEST = 25
MAX_PARTS = 10_000
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

ProgressFn = Callable[[int, int], None]


class ApsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def credentials_from_env() -> tuple[str, str]:
    client_id = (os.environ.get("APS_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("APS_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise ApsError(
            "APS_CLIENT_ID / APS_CLIENT_SECRET are not set. "
            "aps.autodesk.com -> Applications -> Create application (Server-to-Server) -> copy Client ID/Secret "
            "-> Cursor Dashboard -> Cloud Agents -> Secrets (or Vercel project env)"
        )
    return client_id, client_secret


def urn_for(object_id: str) -> str:
    return base64.urlsafe_b64encode(object_id.encode()).decode().rstrip("=")


def default_bucket_key(client_id: str, suffix: str = "takedown") -> str:
    digest = hashlib.sha1(client_id.encode()).hexdigest()[:16]
    return f"{suffix}-{digest}".lower()


def safe_object_key(filename: str) -> str:
    name = Path(filename).name
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


class ApsClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, timeout: float = 120.0):
        if client_id is None or client_secret is None:
            client_id, client_secret = credentials_from_env()
        self.client_id = client_id
        self._secret = client_secret
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    # ---------------- HTTP ----------------
    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 120:
                return self._token
            basic = base64.b64encode(f"{self.client_id}:{self._secret}".encode()).decode()
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPES}).encode()
            data = self._raw(
                "POST",
                f"{BASE_URL}/authentication/v2/token",
                body,
                {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = json.loads(data)
            self._token = payload["access_token"]
            self._token_expiry = time.time() + float(payload.get("expires_in", 3599))
            return self._token

    def _raw(self, method: str, url: str, body: bytes | None, headers: Dict[str, str], retries: int = 5) -> bytes:
        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:2000]
                if exc.code in RETRY_STATUSES and attempt < retries:
                    retry_after = exc.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else _backoff(attempt))
                    continue
                raise ApsError(f"{method} {_redact(url)} -> HTTP {exc.code}: {detail}", exc.code, detail) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise ApsError(f"{method} {_redact(url)} failed: {exc}") from exc
        raise ApsError(f"{method} {_redact(url)} exhausted retries")

    def call(self, method: str, url: str, payload=None, headers: Dict[str, str] | None = None,
             raw_body: bytes | None = None) -> dict:
        all_headers = {"Authorization": f"Bearer {self.token()}"}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        data = self._raw(method, url, body, all_headers)
        return json.loads(data) if data.strip() else {}

    # ---------------- OSS ----------------
    def ensure_bucket(self, bucket_key: str, policy: str = "persistent") -> str:
        try:
            self.call("POST", f"{OSS_URL}/buckets", {"bucketKey": bucket_key, "policyKey": policy})
        except ApsError as exc:
            if exc.status != 409:
                raise
        return bucket_key

    def object_details(self, bucket_key: str, object_key: str) -> dict | None:
        try:
            return self.call("GET", f"{OSS_URL}/buckets/{bucket_key}/objects/{_key(object_key)}/details")
        except ApsError as exc:
            if exc.status == 404:
                return None
            raise

    def signed_upload_urls(self, bucket_key: str, object_key: str, first_part: int, parts: int,
                           upload_key: str | None = None, minutes: int = 60) -> dict:
        query = {"firstPart": first_part, "parts": min(parts, MAX_URLS_PER_REQUEST), "minutesExpiration": minutes}
        if upload_key:
            query["uploadKey"] = upload_key
        return self.call(
            "GET",
            f"{OSS_URL}/buckets/{bucket_key}/objects/{_key(object_key)}/signeds3upload?"
            + urllib.parse.urlencode(query),
        )

    def complete_upload(self, bucket_key: str, object_key: str, upload_key: str, size: int | None = None) -> dict:
        payload: dict = {"uploadKey": upload_key}
        if size is not None:
            payload["size"] = size
        return self.call(
            "POST",
            f"{OSS_URL}/buckets/{bucket_key}/objects/{_key(object_key)}/signeds3upload",
            payload,
        )

    def upload_file(self, bucket_key: str, object_key: str, path: str | Path, *, part_bytes: int = DEFAULT_PART_BYTES,
                    workers: int = 6, state_path: str | Path | None = None, progress: ProgressFn | None = None,
                    skip_if_exists: bool = True) -> dict:
        """Resumable, parallel multipart upload via signed S3 URLs.

        Progress is checkpointed to ``state_path`` after every part, so rerunning the same command
        after a crash or network drop only sends the missing parts (while the upload key is valid).
        """
        path = Path(path)
        size = path.stat().st_size
        if skip_if_exists:
            existing = self.object_details(bucket_key, object_key)
            if existing and int(existing.get("size", -1)) == size:
                return existing

        part_bytes = max(MIN_PART_BYTES, part_bytes, -(-size // MAX_PARTS))
        total_parts = max(1, -(-size // part_bytes))
        state_path = Path(state_path) if state_path else None
        state = _load_state(state_path, size, part_bytes, path)
        done: set[int] = set(state.get("done", []))
        upload_key: str | None = state.get("uploadKey")
        state_lock = threading.Lock()
        sent = sum(_part_length(n, part_bytes, size) for n in done)
        if progress:
            progress(sent, size)

        def save_state():
            if state_path:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps({**state, "uploadKey": upload_key, "done": sorted(done)}))

        def put_part(number: int, url: str) -> int:
            offset = (number - 1) * part_bytes
            length = _part_length(number, part_bytes, size)
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(length)
            self._raw("PUT", url, chunk, {"Content-Type": "application/octet-stream"}, retries=4)
            return length

        while len(done) < total_parts:
            first = min(n for n in range(1, total_parts + 1) if n not in done)
            count = min(MAX_URLS_PER_REQUEST, total_parts - first + 1)
            batch = [n for n in range(first, first + count) if n not in done]
            try:
                signed = self.signed_upload_urls(bucket_key, object_key, first, count, upload_key)
            except ApsError as exc:
                if upload_key and exc.status in (400, 404):
                    upload_key, done = None, set()
                    sent = 0
                    save_state()
                    continue
                raise
            upload_key = signed["uploadKey"]
            urls = {first + i: url for i, url in enumerate(signed["urls"])}
            save_state()
            expired = False
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(put_part, n, urls[n]): n for n in batch if n in urls}
                for future in concurrent.futures.as_completed(futures):
                    number = futures[future]
                    try:
                        length = future.result()
                    except ApsError as exc:
                        if exc.status == 403:
                            expired = True
                            continue
                        raise
                    with state_lock:
                        done.add(number)
                        sent += length
                        save_state()
                    if progress:
                        progress(sent, size)
            if expired:
                continue

        result = self.complete_upload(bucket_key, object_key, upload_key, size)
        if state_path and state_path.exists():
            state_path.unlink()
        return result

    def signed_download_url(self, bucket_key: str, object_key: str, minutes: int = 60) -> str:
        data = self.call(
            "GET",
            f"{OSS_URL}/buckets/{bucket_key}/objects/{_key(object_key)}/signeds3download?minutesExpiration={minutes}",
        )
        if data.get("url"):
            return data["url"]
        raise ApsError(f"signeds3download returned no url (status={data.get('status')})")

    def download_object(self, bucket_key: str, object_key: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self.signed_download_url(bucket_key, object_key)
        _stream_to(url, dest, {}, self.timeout)
        return dest

    def object_urn(self, bucket_key: str, object_key: str) -> str:
        return f"urn:adsk.objects:os.object:{bucket_key}/{urllib.parse.quote(object_key, safe='')}"

    # ---------------- Design Automation ----------------
    def nickname(self) -> str:
        data = self._raw("GET", f"{DA_URL}/forgeapps/me", None, {"Authorization": f"Bearer {self.token()}"})
        return json.loads(data)

    def engine(self, engine_id: str) -> dict:
        return self.call("GET", f"{DA_URL}/engines/{urllib.parse.quote(engine_id)}")

    def service_limits(self) -> dict:
        return self.call("GET", f"{DA_URL}/servicelimits/me")

    def ensure_appbundle(self, name: str, engine_id: str, zip_path: str | Path, alias: str = "prod",
                         description: str = "") -> dict:
        spec = {"engine": engine_id, "description": description}
        try:
            created = self.call("POST", f"{DA_URL}/appbundles", {"id": name, **spec})
        except ApsError as exc:
            if exc.status != 409:
                raise
            created = self.call("POST", f"{DA_URL}/appbundles/{name}/versions", spec)
        _post_form(created["uploadParameters"], Path(zip_path), self.timeout)
        self._set_alias("appbundles", name, alias, created["version"])
        return created

    def ensure_activity(self, activity: dict, alias: str = "prod") -> dict:
        name = activity["id"]
        body = {k: v for k, v in activity.items() if k != "id"}
        try:
            created = self.call("POST", f"{DA_URL}/activities", activity)
        except ApsError as exc:
            if exc.status != 409:
                raise
            created = self.call("POST", f"{DA_URL}/activities/{name}/versions", body)
        self._set_alias("activities", name, alias, created["version"])
        return created

    def _set_alias(self, kind: str, name: str, alias: str, version: int) -> None:
        try:
            self.call("POST", f"{DA_URL}/{kind}/{name}/aliases", {"id": alias, "version": version})
        except ApsError as exc:
            if exc.status != 409:
                raise
            self.call("PATCH", f"{DA_URL}/{kind}/{name}/aliases/{alias}", {"version": version})

    def submit_workitem(self, activity_id: str, arguments: dict, limit_seconds: int | None = None) -> dict:
        payload: dict = {"activityId": activity_id, "arguments": arguments}
        if limit_seconds:
            payload["limitProcessingTimeSec"] = limit_seconds
        if len(json.dumps(payload)) > 16_000:
            raise ApsError("Workitem payload exceeds the 16 KB Design Automation limit")
        return self.call("POST", f"{DA_URL}/workitems", payload)

    def workitem(self, workitem_id: str) -> dict:
        return self.call("GET", f"{DA_URL}/workitems/{workitem_id}")

    def wait_workitem(self, workitem_id: str, timeout_s: float = 4 * 3600, on_status: Callable[[dict], None] | None = None) -> dict:
        return _poll(lambda: self.workitem(workitem_id), lambda s: s.get("status") not in {"pending", "inprogress"},
                     timeout_s, on_status)

    def fetch_text(self, url: str) -> str:
        return self._raw("GET", url, None, {}).decode("utf-8", errors="replace")

    # ---------------- Model Derivative ----------------
    def start_translation(self, urn: str, formats: List[dict], root_filename: str | None = None, force: bool = False) -> dict:
        job: dict = {"input": {"urn": urn}, "output": {"formats": formats}}
        if root_filename:
            job["input"].update({"compressedUrn": True, "rootFilename": root_filename})
        headers = {"x-ads-force": "true"} if force else {}
        return self.call("POST", f"{MD_URL}/designdata/job", job, headers)

    def manifest(self, urn: str) -> dict:
        return self.call("GET", f"{MD_URL}/designdata/{urn}/manifest")

    def wait_manifest(self, urn: str, timeout_s: float = 4 * 3600, on_status: Callable[[dict], None] | None = None) -> dict:
        return _poll(lambda: self.manifest(urn), lambda m: m.get("status") in {"success", "failed", "timeout"},
                     timeout_s, on_status)

    def download_derivative(self, urn: str, derivative_urn: str, dest: str | Path) -> Path:
        encoded = urllib.parse.quote(derivative_urn, safe="")
        request = urllib.request.Request(
            f"{MD_URL}/designdata/{urn}/manifest/{encoded}/signedcookies",
            headers={"Authorization": f"Bearer {self.token()}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            info = json.loads(response.read())
            cookies = "; ".join(c.split(";", 1)[0] for c in response.headers.get_all("Set-Cookie") or [])
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _stream_to(info["url"], dest, {"Cookie": cookies}, self.timeout)
        return dest


def iter_derivatives(manifest: dict, output_type: str | None = None, mime: str | None = None) -> Iterable[dict]:
    def walk(nodes, parent_type=None):
        for node in nodes or []:
            kind = node.get("outputType") or parent_type
            if node.get("urn") and node.get("role") != "thumbnail":
                if (output_type is None or kind == output_type) and (mime is None or node.get("mime") == mime):
                    yield node
            yield from walk(node.get("children"), kind)
    yield from walk(manifest.get("derivatives"))


# ---------------- helpers ----------------
def _key(object_key: str) -> str:
    return urllib.parse.quote(object_key, safe="")


def _backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** attempt) + random.random()


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


def _part_length(number: int, part_bytes: int, size: int) -> int:
    return max(0, min(part_bytes, size - (number - 1) * part_bytes))


def _load_state(state_path: Path | None, size: int, part_bytes: int, path: Path) -> dict:
    fingerprint = {"size": size, "partBytes": part_bytes, "mtime": int(path.stat().st_mtime), "name": path.name}
    if state_path and state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            if all(state.get(k) == v for k, v in fingerprint.items()):
                return state
        except (OSError, ValueError):
            pass
    return dict(fingerprint)


def _poll(fetch, finished, timeout_s: float, on_status=None, first: float = 10.0, cap: float = 60.0):
    deadline = time.time() + timeout_s
    delay = first
    while True:
        status = fetch()
        if on_status:
            on_status(status)
        if finished(status):
            return status
        if time.time() > deadline:
            raise ApsError(f"Timed out after {timeout_s:.0f}s waiting for job (last status: {status.get('status')})")
        time.sleep(delay)
        delay = min(cap, delay * 1.5)


def _stream_to(url: str, dest: Path, headers: dict, timeout: float) -> None:
    request = urllib.request.Request(url, headers=headers)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(8 * 1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    tmp.replace(dest)


def _post_form(upload: dict, file_path: Path, timeout: float) -> None:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in upload["formData"].items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    body = b"".join(parts) + file_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        upload["endpointURL"], data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        raise ApsError(f"AppBundle upload failed: HTTP {exc.code}: {exc.read()[:500]!r}", exc.code) from exc
