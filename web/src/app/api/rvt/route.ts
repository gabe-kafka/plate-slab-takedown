import { NextResponse } from "next/server";
import { createHash, randomUUID } from "crypto";
import { getOptionalSession } from "@/auth";
import {
  ApsHttpError,
  apsConfigured,
  completeUpload,
  ensureBucket,
  partPlan,
  signedUploadUrls,
  submitTakedownWorkitem,
  workitemStatus,
} from "@/lib/aps";

export const runtime = "nodejs";
export const maxDuration = 60;

type Body =
  | { action: "start"; filename: string; size: number }
  | { action: "urls"; objectKey: string; uploadKey: string; firstPart: number; parts: number }
  | { action: "complete"; objectKey: string; uploadKey: string; size: number; levels?: string[]; hostFile?: string }
  | { action: "status"; workitemId: string; resultKey: string; levels?: string[]; filename?: string };

function userPrefix(userId: string): string {
  return `uploads/${createHash("sha1").update(userId).digest("hex").slice(0, 12)}/`;
}

function safeName(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").slice(-120);
}

export async function POST(request: Request) {
  if (!apsConfigured()) {
    return NextResponse.json(
      { detail: "Revit upload is not configured (APS_CLIENT_ID / APS_CLIENT_SECRET missing)." },
      { status: 503 },
    );
  }
  const session = await getOptionalSession();
  const userId = session?.user && (session.user as { id?: string }).id;
  if (!userId) return NextResponse.json({ detail: "Sign in to convert Revit models." }, { status: 401 });

  const body = (await request.json()) as Body;
  const prefix = userPrefix(userId);
  const owns = (key: string) => key.startsWith(prefix) && !key.includes("..");

  try {
    switch (body.action) {
      case "start": {
        const lower = body.filename.toLowerCase();
        if (!lower.endsWith(".rvt") && !lower.endsWith(".zip")) {
          return NextResponse.json({ detail: "Upload a .rvt (or a .zip of host + linked models)." }, { status: 400 });
        }
        await ensureBucket();
        const objectKey = `${prefix}${randomUUID().slice(0, 8)}-${safeName(body.filename)}`;
        const { partBytes, totalParts } = partPlan(body.size);
        const signed = await signedUploadUrls(objectKey, 1, totalParts);
        return NextResponse.json({ objectKey, partBytes, totalParts, uploadKey: signed.uploadKey, urls: signed.urls });
      }
      case "urls": {
        if (!owns(body.objectKey)) return NextResponse.json({ detail: "Forbidden" }, { status: 403 });
        const signed = await signedUploadUrls(body.objectKey, body.firstPart, body.parts, body.uploadKey);
        return NextResponse.json({ uploadKey: signed.uploadKey, urls: signed.urls });
      }
      case "complete": {
        if (!owns(body.objectKey)) return NextResponse.json({ detail: "Forbidden" }, { status: 403 });
        await completeUpload(body.objectKey, body.uploadKey, body.size);
        const resultKey = `results/${body.objectKey.slice("uploads/".length)}-${Date.now()}.zip`;
        const workitem = await submitTakedownWorkitem(body.objectKey, resultKey, {
          levels: body.levels ?? [],
          hostFile: body.hostFile ?? null,
        });
        return NextResponse.json({ workitemId: workitem.id, resultKey });
      }
      case "status": {
        if (!owns(`uploads/${body.resultKey.slice("results/".length)}`)) {
          return NextResponse.json({ detail: "Forbidden" }, { status: 403 });
        }
        const status = await workitemStatus(body.workitemId);
        if (status.status === "pending" || status.status === "inprogress") {
          return NextResponse.json({ state: status.status, progress: status.progress ?? null });
        }
        if (status.status !== "success") {
          return NextResponse.json({ state: "failed", detail: `Revit job ${status.status}`, reportUrl: status.reportUrl ?? null });
        }
        const convert = await fetch(new URL("/api/rvt_convert", request.url), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ result_key: body.resultKey, levels: body.levels ?? [], filename: body.filename }),
        });
        const converted = await convert.json();
        if (!convert.ok) return NextResponse.json({ state: "failed", detail: converted.detail ?? "DXF build failed" });
        return NextResponse.json({ state: "done", draft: converted });
      }
    }
  } catch (e) {
    const status = e instanceof ApsHttpError ? 502 : 500;
    return NextResponse.json({ detail: e instanceof Error ? e.message : "APS request failed" }, { status });
  }
  return NextResponse.json({ detail: "Unknown action" }, { status: 400 });
}
