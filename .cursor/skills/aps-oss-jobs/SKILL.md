---
name: aps-oss-jobs
description: Autodesk Platform Services building blocks in Python - 2-legged OAuth, OSS bucket + resumable parallel signed-S3 multipart upload/download, Design Automation AppBundle/Activity/WorkItem with polling, Model Derivative jobs and derivative download. Use when scripting any APS upload, translation or automation job, especially for large (GB) files.
---

# APS auth, OSS upload, job polling

Client: `web/api/_engine/aps_client.py`. It is stdlib only, so it can be imported from the CLI, tests and Vercel functions.

```python
import sys; sys.path.insert(0, "web/api/_engine")
from aps_client import ApsClient, default_bucket_key, safe_object_key, urn_for, iter_derivatives
c = ApsClient()                                   # reads APS_CLIENT_ID / APS_CLIENT_SECRET; token cached + auto-refreshed
bucket = c.ensure_bucket(default_bucket_key(c.client_id))   # 409 = already yours
c.upload_file(bucket, safe_object_key(path), path, part_bytes=64 << 20, workers=6,
              state_path=".cache/upload.json", progress=lambda s, t: print(s, t))
```

## Upload rules (direct-to-S3)
- `GET .../signeds3upload?firstPart&parts&uploadKey&minutesExpiration` returns ≤25 URLs per call, valid 1–60 min.
- Parts must be ≥5 MB except the last, with ≤10,000 parts. `upload_file` enlarges parts to stay under the cap.
- PUT the bytes to each URL. A 403 means the URL expired: re-sign with the same `uploadKey`. 429/5xx is retried with backoff.
- Finish with `POST .../signeds3upload {uploadKey, size}`.
- The state file records the finished parts, so a rerun sends only the missing ones. An existing object of the same size is skipped.
- Object keys with `/` must be URL-encoded (`%2F`) in OSS paths.
- Browser uploads can PUT to the same signed URLs; see `web/src/lib/rvt.ts` and `web/src/app/api/rvt/route.ts`.

## Design Automation
```python
nick = c.nickname()
c.ensure_appbundle("Name", "Autodesk.Revit+2026", "bundle.zip")          # create or new version, alias prod
c.ensure_activity({"id": "Name", "engine": ..., "appbundles": [f"{nick}.Name+prod"], "commandLine": [...], "parameters": {...}})
wi = c.submit_workitem(f"{nick}.Name+prod", {
    "in":  {"url": c.object_urn(bucket, key), "verb": "get", "headers": {"Authorization": f"Bearer {c.token()}"}},
    "out": {"url": c.object_urn(bucket, "results/x.zip"), "verb": "put", "headers": {...}},
}, limit_seconds=3 * 3600)
status = c.wait_workitem(wi["id"], on_status=print)   # backoff 10 s → 60 s; then status["reportUrl"]
```
- Pass OSS URNs rather than signed URLs: DA downloads and uploads itself (multipart) and extends the token, which suits long queues and GB inputs.
- The workitem JSON must be ≤16 KB, and bearer tokens count toward it. `data:application/json,{...}` works for small params.
- Check `c.engine(id)` and `c.service_limits()` before assuming engine or version availability.
- Cost is 2 Flex tokens per processing hour. Prefer small activities and use `--no-views`-style flags to skip expensive exports.

## Model Derivative
```python
urn = urn_for(f"urn:adsk.objects:os.object:{bucket}/{key}")
c.start_translation(urn, [{"type": "dwg", "views": ["2d"]}], root_filename=None, force=True)  # zip: root_filename="host.rvt"
m = c.wait_manifest(urn)
for node in iter_derivatives(m, output_type="dwg"): c.download_derivative(urn, node["urn"], out / node["urn"].split("/")[-1])
```
RVT→DWG exports **sheets only**. Revit counts as a "complex" job for billing.

## Testing without credentials
`tests/rvt2dxf/fake_aps.py` is an in-process fake of the token, OSS, S3 part and DA endpoints. Monkeypatch `aps_client.BASE_URL/DA_URL/OSS_URL/MD_URL` to `FakeAps().base` (see `tests/rvt2dxf/test_pipeline.py`).
