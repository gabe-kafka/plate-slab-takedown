"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { type DemoId, uploadDemo, uploadDxf } from "@/lib/api";
import { layerMappingFromDraft } from "@/lib/layerMapping";
import type { DraftData } from "@/lib/types";

const DEMO_OPTIONS: { id: DemoId; label: string; filename: string }[] = [
  { id: "default", label: "Load 358 Demo 1 (good example)", filename: "358 Flatbush - input.dxf" },
  { id: "geom_clean_1", label: "Load 1025 Demo 2 (good example)", filename: "1025 Atlantic - input.dxf" },
  { id: "fulton_356", label: "Load 356 Demo 3 (best reference)", filename: "356 Fulton - input.dxf" },
  { id: "franklin_246", label: "Load 246 Demo 4", filename: "246 Franklin - input.dxf" },
  { id: "manhattan_1300", label: "Load 1300 Demo 5 (hillside, arch background)", filename: "1300 Manhattan - input.dxf" },
];
const LANDING_DEMO = DEMO_OPTIONS[2];

interface UploadFlowProps {
  forceUploader?: boolean;
}

function UploadFlowInner({ forceUploader = false }: UploadFlowProps) {
  const router = useRouter();
  const params = useSearchParams();
  const expired = params.get("expired") === "1";
  const showUploader = forceUploader || params.get("upload") === "1" || expired;
  const fileRef = useRef<HTMLInputElement>(null);
  const launchedLandingDemo = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(
    expired ? "Session expired — please re-upload your DXF." : null,
  );
  const [filename, setFilename] = useState<string | null>(null);

  const handleDraft = useCallback(
    (draft: DraftData) => {
      sessionStorage.setItem("draft", JSON.stringify(draft));
      router.push("/review");
    },
    [router],
  );

  const launchLandingDemo = useCallback(async () => {
    launchedLandingDemo.current = true;
    setError(null);
    setFilename(LANDING_DEMO.filename);
    setLoading(true);

    try {
      const draft = await uploadDemo(LANDING_DEMO.id);
      sessionStorage.removeItem("draft");
      sessionStorage.removeItem("openedProject");
      sessionStorage.setItem(
        "processParams",
        JSON.stringify({
          blob_url: draft.blob_url,
          source_units: "in",
          layer_mapping: layerMappingFromDraft(draft),
          view_mode: "iso",
          dxf_filename: LANDING_DEMO.filename,
          dxf_size_bytes: null,
        }),
      );
      router.replace("/job");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Demo failed.");
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    if (showUploader || launchedLandingDemo.current) return;
    const timeoutId = window.setTimeout(() => {
      void launchLandingDemo();
    }, 0);

    return () => window.clearTimeout(timeoutId);
  }, [launchLandingDemo, showUploader]);

  const retryLandingDemo = useCallback(() => {
    launchedLandingDemo.current = false;
    void launchLandingDemo();
  }, [launchLandingDemo]);

  const handleFile = useCallback(
    async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".dxf")) {
        setError("Upload a .dxf file.");
        return;
      }
      setError(null);
      setFilename(file.name);
      setLoading(true);
      try {
        const draft = await uploadDxf(file);
        sessionStorage.removeItem("openedProject");
        handleDraft(draft);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Upload failed.");
        setLoading(false);
      }
    },
    [handleDraft],
  );

  const handleDemo = useCallback(async (demo: (typeof DEMO_OPTIONS)[number]) => {
    setError(null);
    setFilename(demo.filename);
    setLoading(true);
    try {
      const draft = await uploadDemo(demo.id);
      sessionStorage.removeItem("openedProject");
      handleDraft(draft);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Demo failed.");
      setLoading(false);
    }
  }, [handleDraft]);

  if (!showUploader) {
    return (
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="w-full max-w-lg space-y-6">
          <div className="space-y-1">
            <h1 className="text-lg font-semibold text-text-primary">
              Loading 356 Fulton Demo
            </h1>
            <p className="text-text-secondary text-[12px]">
              Preparing the Demo 3 ISO model.
            </p>
          </div>

          <div className="border border-border-panel bg-bg-surface px-4 py-3">
            <div className="h-1 bg-bg-panel overflow-hidden mb-3">
              {loading && <div className="h-full bg-accent animate-pulse w-full" />}
            </div>
            <div className="flex items-center justify-between gap-3 text-[11px]">
              <span className="text-text-muted">
                {filename ?? LANDING_DEMO.filename}
              </span>
              <span className="text-text-secondary">
                {loading ? "Processing..." : "Ready"}
              </span>
            </div>
          </div>

          {error && (
            <div className="space-y-3">
              <div className="text-error text-[12px] bg-error/10 border border-error/20 px-3 py-2">
                {error}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={retryLandingDemo}
                  className="px-4 py-1.5 text-[12px] bg-accent text-white hover:bg-accent-hover transition-colors"
                >
                  Retry Demo
                </button>
                <Link
                  href="/?upload=1"
                  className="px-4 py-1.5 text-[12px] border border-border-panel text-text-secondary hover:text-text-primary transition-colors"
                >
                  Upload DXF
                </Link>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="w-full max-w-lg space-y-6">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold text-text-primary">
            Upload DXF
          </h1>
          <p className="text-text-secondary text-[12px]">
            Upload a two-way slab plan to compute tributary areas.
          </p>
        </div>

        {/* Drop zone */}
        <div
          className={`border-2 border-dashed p-8 text-center cursor-pointer transition-colors ${
            dragging
              ? "border-accent bg-accent/5"
              : "border-border-panel hover:border-text-muted"
          }`}
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const file = e.dataTransfer.files[0];
            if (file) handleFile(file);
          }}
        >
          <input
            ref={fileRef}
            type="file"
            accept=".dxf"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleFile(file);
            }}
          />
          {loading ? (
            <div className="space-y-2">
              <div className="text-accent animate-pulse">Processing...</div>
              <div className="text-text-muted text-[11px]">{filename}</div>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-text-secondary">
                Drop .dxf file here or click to browse
              </div>
              {filename && (
                <div className="text-text-muted text-[11px]">{filename}</div>
              )}
            </div>
          )}
        </div>

        {error && (
          <div className="text-error text-[12px] bg-error/10 border border-error/20 px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex items-center gap-3">
          <div className="flex-1 border-t border-border-panel" />
          <span className="text-text-muted text-[11px]">or</span>
          <div className="flex-1 border-t border-border-panel" />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {DEMO_OPTIONS.map((demo) => (
            <button
              key={demo.id}
              onClick={() => handleDemo(demo)}
              disabled={loading}
              className="w-full py-2 px-4 border border-border-panel text-text-secondary hover:text-text-primary hover:border-text-muted transition-colors disabled:opacity-40 text-[12px]"
            >
              {demo.label}
            </button>
          ))}
        </div>

        <a
          href="/template.dxf"
          download="reference.dxf"
          className="block w-full py-2 px-4 border border-border-panel text-text-secondary hover:text-text-primary hover:border-text-muted transition-colors text-center text-[12px]"
        >
          Download Reference DXF
        </a>
      </div>
    </div>
  );
}

export default function UploadFlow(props: UploadFlowProps) {
  return (
    <Suspense>
      <UploadFlowInner {...props} />
    </Suspense>
  );
}
