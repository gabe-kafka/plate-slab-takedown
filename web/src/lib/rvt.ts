import type { DraftData } from "./types";

const CONCURRENCY = 4;
const PART_RETRIES = 4;
const POLL_MS = 15_000;

export type RvtProgress =
  | { stage: "upload"; sentBytes: number; totalBytes: number }
  | { stage: "revit"; state: string; elapsedS: number }
  | { stage: "dxf" };

async function rvtApi<T>(body: object): Promise<T> {
  const res = await fetch("/api/rvt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ?? `${res.status}`);
  return data as T;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Browser -> APS OSS direct-to-S3 multipart upload, then Design Automation, then DXF draft. */
export async function convertRvt(
  file: File,
  levels: string[],
  onProgress: (p: RvtProgress) => void,
  hostFile?: string,
): Promise<DraftData> {
  const start = await rvtApi<{ objectKey: string; partBytes: number; totalParts: number; uploadKey: string; urls: string[] }>(
    { action: "start", filename: file.name, size: file.size },
  );
  const { objectKey, partBytes, totalParts } = start;
  let uploadKey = start.uploadKey;
  const urls = new Map<number, string>(start.urls.map((u, i) => [i + 1, u]));
  let sent = 0;

  async function refreshUrls(first: number) {
    const parts = Math.min(25, totalParts - first + 1);
    const fresh = await rvtApi<{ uploadKey: string; urls: string[] }>({ action: "urls", objectKey, uploadKey, firstPart: first, parts });
    uploadKey = fresh.uploadKey;
    fresh.urls.forEach((u, i) => urls.set(first + i, u));
  }

  async function putPart(n: number) {
    const blob = file.slice((n - 1) * partBytes, Math.min(file.size, n * partBytes));
    for (let attempt = 0; ; attempt++) {
      if (!urls.has(n)) await refreshUrls(n);
      const res = await fetch(urls.get(n)!, { method: "PUT", body: blob }).catch(() => null);
      if (res?.ok) break;
      if (res?.status === 403) urls.delete(n);
      if (attempt >= PART_RETRIES) throw new Error(`Upload of part ${n} failed (${res?.status ?? "network"})`);
      await sleep(1000 * 2 ** attempt);
    }
    sent += blob.size;
    onProgress({ stage: "upload", sentBytes: sent, totalBytes: file.size });
  }

  let next = 1;
  await Promise.all(
    Array.from({ length: Math.min(CONCURRENCY, totalParts) }, async () => {
      while (next <= totalParts) await putPart(next++);
    }),
  );

  const job = await rvtApi<{ workitemId: string; resultKey: string }>({
    action: "complete", objectKey, uploadKey, size: file.size, levels, hostFile,
  });
  const started = Date.now();
  for (;;) {
    const status = await rvtApi<{ state: string; draft?: DraftData; detail?: string }>({
      action: "status", workitemId: job.workitemId, resultKey: job.resultKey, levels, filename: file.name,
    });
    if (status.state === "done" && status.draft) return status.draft;
    if (status.state === "failed") throw new Error(status.detail ?? "Revit conversion failed");
    onProgress({ stage: "revit", state: status.state, elapsedS: (Date.now() - started) / 1000 });
    await sleep(POLL_MS);
  }
}
