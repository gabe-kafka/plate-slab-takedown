import { createHash } from "crypto";

const BASE = "https://developer.api.autodesk.com";
const DA = `${BASE}/da/us-east/v3`;
const OSS = `${BASE}/oss/v2`;
const SCOPES = "data:read data:write data:create bucket:create bucket:read code:all";

export const MAX_URLS_PER_REQUEST = 25;
export const MIN_PART_BYTES = 5 * 1024 * 1024;
export const DEFAULT_PART_BYTES = 64 * 1024 * 1024;
export const MAX_PARTS = 10_000;
export const ACTIVITY = "TakedownExport";

let cached: { token: string; expires: number } | null = null;

export function apsConfigured(): boolean {
  return Boolean(process.env.APS_CLIENT_ID && process.env.APS_CLIENT_SECRET);
}

export class ApsHttpError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

export async function apsToken(): Promise<string> {
  if (cached && Date.now() < cached.expires - 120_000) return cached.token;
  const basic = Buffer.from(`${process.env.APS_CLIENT_ID}:${process.env.APS_CLIENT_SECRET}`).toString("base64");
  const res = await fetch(`${BASE}/authentication/v2/token`, {
    method: "POST",
    headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "client_credentials", scope: SCOPES }),
  });
  if (!res.ok) throw new ApsHttpError(`APS token failed: ${res.status}`, res.status);
  const data = (await res.json()) as { access_token: string; expires_in: number };
  cached = { token: data.access_token, expires: Date.now() + data.expires_in * 1000 };
  return data.access_token;
}

async function aps<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${await apsToken()}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new ApsHttpError(`${method} ${url.split("?")[0]} -> ${res.status}: ${text.slice(0, 300)}`, res.status);
  return (text ? JSON.parse(text) : {}) as T;
}

/** Same derivation as aps_client.default_bucket_key so the CLI and web app share a bucket. */
export function bucketKey(): string {
  const digest = createHash("sha1").update(process.env.APS_CLIENT_ID ?? "").digest("hex").slice(0, 16);
  return `takedown-${digest}`;
}

const enc = (key: string) => encodeURIComponent(key);

export async function ensureBucket(): Promise<string> {
  const key = bucketKey();
  try {
    await aps("POST", `${OSS}/buckets`, { bucketKey: key, policyKey: "persistent" });
  } catch (e) {
    if (!(e instanceof ApsHttpError && e.status === 409)) throw e;
  }
  return key;
}

export function partPlan(size: number): { partBytes: number; totalParts: number } {
  const partBytes = Math.max(DEFAULT_PART_BYTES, MIN_PART_BYTES, Math.ceil(size / MAX_PARTS));
  return { partBytes, totalParts: Math.max(1, Math.ceil(size / partBytes)) };
}

export async function signedUploadUrls(
  objectKey: string,
  firstPart: number,
  parts: number,
  uploadKey?: string,
): Promise<{ uploadKey: string; urls: string[] }> {
  const q = new URLSearchParams({
    firstPart: String(firstPart),
    parts: String(Math.min(parts, MAX_URLS_PER_REQUEST)),
    minutesExpiration: "60",
  });
  if (uploadKey) q.set("uploadKey", uploadKey);
  return aps("GET", `${OSS}/buckets/${bucketKey()}/objects/${enc(objectKey)}/signeds3upload?${q}`);
}

export async function completeUpload(objectKey: string, uploadKey: string, size: number) {
  return aps<{ objectId: string; size: number }>(
    "POST",
    `${OSS}/buckets/${bucketKey()}/objects/${enc(objectKey)}/signeds3upload`,
    { uploadKey, size },
  );
}

export function objectUrn(objectKey: string): string {
  return `urn:adsk.objects:os.object:${bucketKey()}/${enc(objectKey)}`;
}

export async function submitTakedownWorkitem(
  objectKey: string,
  resultKey: string,
  params: { levels: string[]; hostFile?: string | null },
  limitSeconds = 3 * 3600,
): Promise<{ id: string }> {
  const token = await apsToken();
  const nickname = await aps<string>("GET", `${DA}/forgeapps/me`);
  const auth = { Authorization: `Bearer ${token}` };
  const rvtFile: Record<string, unknown> = { url: objectUrn(objectKey), verb: "get", headers: auth };
  if (objectKey.toLowerCase().endsWith(".zip")) {
    rvtFile.localName = "input";
    if (params.hostFile) rvtFile.pathInZip = params.hostFile;
  }
  const daParams = { levels: params.levels, includeLinks: true, exportViews: false, includeArchColumns: true, hostFile: params.hostFile ?? null };
  return aps("POST", `${DA}/workitems`, {
    activityId: `${nickname}.${ACTIVITY}+prod`,
    limitProcessingTimeSec: limitSeconds,
    arguments: {
      rvtFile,
      params: { url: `data:application/json,${JSON.stringify(daParams)}` },
      result: { url: objectUrn(resultKey), verb: "put", headers: auth },
    },
  });
}

export interface WorkitemStatus {
  id: string;
  status: string;
  progress?: string;
  reportUrl?: string;
  stats?: Record<string, string>;
}

export async function workitemStatus(id: string): Promise<WorkitemStatus> {
  return aps("GET", `${DA}/workitems/${encodeURIComponent(id)}`);
}
