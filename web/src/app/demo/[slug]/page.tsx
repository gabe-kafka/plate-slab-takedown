"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { processJob, uploadDemo } from "@/lib/api";
import { DEMO_PROJECTS, findDemo } from "@/lib/demos";
import { layerMappingFromDraft } from "@/lib/layerMapping";
import type { ProcessResult } from "@/lib/types";

const ResultsView = dynamic(() => import("@/components/ResultsView"), {
  ssr: false,
  loading: () => (
    <div className="flex-1 flex items-center justify-center text-text-muted">
      Loading canvas...
    </div>
  ),
});

/**
 * /demo/<slug>: one shareable URL per demo project. Loads the bundled DXF,
 * runs the engine with the canonical layer mapping, and opens the result in
 * ISO view with tributaries on. No clicks needed.
 */
export default function DemoPage() {
  const params = useParams<{ slug: string }>();
  const demo = findDemo(String(params?.slug ?? ""));
  const [result, setResult] = useState<ProcessResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stage, setStage] = useState<"loading" | "computing" | "done">("loading");
  const started = useRef(false);

  useEffect(() => {
    if (!demo || started.current) return;
    started.current = true;
    document.title = `${demo.name} · Tributary Areas`;
    (async () => {
      try {
        // Precomputed result (scripts/precompute_demos.py) opens instantly;
        // fall back to the live engine when it is missing.
        const cached = await fetch(`/demos/${demo.slug}/result.json`, { cache: "force-cache" });
        if (cached.ok) {
          const res = (await cached.json()) as ProcessResult;
          if (res.status === "completed" && res.geometry) {
            setResult(res);
            return;
          }
        }
        const draft = await uploadDemo(demo.id);
        setStage("computing");
        const res = await processJob(draft.blob_url, "in", layerMappingFromDraft(draft));
        setResult(res);
        if (res.status !== "completed" || !res.geometry) {
          setError(res.error_message || `Engine finished with status ${res.status}.`);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Demo failed.");
      } finally {
        setStage("done");
      }
    })();
  }, [demo]);

  if (!demo) {
    return (
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-lg space-y-4">
          <h1 className="text-sm font-semibold text-text-primary">No such demo</h1>
          <p className="text-text-secondary text-[12px]">These projects have a direct link:</p>
          <ul className="space-y-1 text-[12px]">
            {DEMO_PROJECTS.map((d) => (
              <li key={d.slug}>
                <Link href={`/demo/${d.slug}`} className="text-accent hover:underline">
                  /demo/{d.slug}
                </Link>
                <span className="text-text-muted"> {d.name}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  if (result?.status === "completed" && result.geometry) {
    return (
      <div className="relative flex-1 flex flex-col">
        <ResultsView
          result={result}
          geometry={result.geometry}
          initialViewMode="iso"
          initialShowTributaries
        />
      </div>
    );
  }

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="w-full max-w-lg space-y-6">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold text-text-primary">{demo.name}</h1>
          <p className="text-text-secondary text-[12px]">
            {stage === "loading"
              ? "Reading the drawing..."
              : stage === "computing"
                ? "Computing tributary areas for every floor. About a minute."
                : error
                  ? "The demo did not finish."
                  : "Ready."}
          </p>
        </div>
        <div className="border border-border-panel bg-bg-surface px-4 py-3">
          <div className="h-1 bg-bg-panel overflow-hidden mb-3">
            {stage !== "done" && <div className="h-full bg-accent animate-pulse w-full" />}
          </div>
          <div className="flex items-center justify-between gap-3 text-[11px]">
            <span className="text-text-muted">{demo.filename}</span>
            <span className="text-text-secondary">
              {stage === "loading" ? "Loading..." : stage === "computing" ? "Computing..." : error ? "Failed" : "Done"}
            </span>
          </div>
        </div>
        {error && (
          <div className="space-y-3">
            <div className="text-error text-[12px] bg-error/10 border border-error/20 px-3 py-2">
              {error}
            </div>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="px-4 py-1.5 text-[12px] bg-accent text-white hover:bg-accent-hover transition-colors"
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
