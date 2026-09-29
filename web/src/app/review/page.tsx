"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  EMPTY_LAYER_MAPPING,
  layerMappingFromDraft,
} from "@/lib/layerMapping";
import type { DraftData, LayerMapping } from "@/lib/types";

const ROLES = [
  { key: "boundary", label: "Boundary", required: true },
  { key: "additional_load", label: "Additional Load", required: false },
  { key: "opening", label: "Slab Openings", required: false },
  { key: "support_point", label: "Columns / Points", required: true },
  { key: "wall", label: "Walls", required: false },
  { key: "beam", label: "Beams", required: false },
  { key: "column_label", label: "Column Labels", required: false },
  { key: "floor_label", label: "Floor Labels", required: false },
  { key: "datum", label: "Datum", required: false },
] as const;

function readDraftFromSession(): DraftData | null {
  if (typeof window === "undefined") return null;
  const raw = sessionStorage.getItem("draft");
  return raw ? (JSON.parse(raw) as DraftData) : null;
}

function mappingFromDraft(draft: DraftData | null): LayerMapping {
  return draft ? layerMappingFromDraft(draft) : EMPTY_LAYER_MAPPING;
}

const SLAB_ROLES: ReadonlyArray<keyof LayerMapping> = ["boundary", "additional_load"];

function entitySummary(counts: { [type: string]: number }): string {
  return Object.entries(counts)
    .map(([t, n]) => `${n} ${t}`)
    .join(", ");
}

export default function ReviewPage() {
  const router = useRouter();
  const [draft, setDraft] = useState<DraftData | null>(null);
  const [mapping, setMapping] = useState<LayerMapping>(EMPTY_LAYER_MAPPING);
  const [units, setUnits] = useState("in");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const nextDraft = readDraftFromSession();
    if (!nextDraft) {
      router.replace("/");
      return;
    }

    const timeoutId = window.setTimeout(() => {
      setDraft(nextDraft);
      setMapping(mappingFromDraft(nextDraft));
    }, 0);

    return () => window.clearTimeout(timeoutId);
  }, [router]);

  const toggleLayer = useCallback(
    (role: keyof LayerMapping, layer: string) => {
      setMapping((prev) => {
        const current = prev[role];
        const next = current.includes(layer)
          ? current.filter((l) => l !== layer)
          : [...current, layer];
        const updated: LayerMapping = { ...prev, [role]: next };

        // Boundary and Additional Load both define slab area — a layer
        // belongs to one or the other, never both.
        if (SLAB_ROLES.includes(role) && !current.includes(layer)) {
          const other = SLAB_ROLES.find((r) => r !== role)!;
          updated[other] = prev[other].filter((l) => l !== layer);
        }

        return updated;
      });
    },
    [],
  );

  const kpis = useMemo(() => {
    if (!draft) return [];
    return [
      { label: "File", value: draft.filename },
      {
        label: "Area (est.)",
        value: `${Math.round(draft.inferred_floor_area_sf).toLocaleString()} SF`,
      },
      {
        label: "Layer Pick",
        value: draft.suggestion_source === "ai" ? "AI" : "Rules",
      },
      { label: "Layers", value: String(draft.layers.length) },
    ];
  }, [draft]);

  const handleSubmit = useCallback(() => {
    if (!draft) return;
    if (!mapping.boundary.length || !mapping.support_point.length) {
      setError("Select at least one boundary layer and one column layer.");
      return;
    }
    setError(null);
    setLoading(true);
    sessionStorage.setItem(
      "processParams",
      JSON.stringify({
        blob_url: draft.blob_url,
        source_units: units,
        layer_mapping: mapping,
        dxf_filename: draft.filename,
        dxf_size_bytes: null,
      }),
    );
    router.push("/job");
  }, [draft, mapping, units, router]);

  if (!draft) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-muted">
        Loading...
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col p-4 gap-4 max-w-5xl mx-auto w-full">
      {/* KPI strip */}
      <div className="flex gap-4">
        {kpis.map((kpi) => (
          <div
            key={kpi.label}
            className="flex-1 bg-bg-surface border border-border-panel px-3 py-2"
          >
            <div className="text-text-muted text-[10px] uppercase tracking-wider">
              {kpi.label}
            </div>
            <div className="text-text-primary text-sm font-medium truncate">
              {kpi.value}
            </div>
          </div>
        ))}
      </div>

      {/* Unit selection */}
      <div className="bg-bg-surface border border-border-panel px-3 py-2 flex items-center gap-4">
        <span className="text-text-secondary text-[12px]">Source units:</span>
        {["in", "ft"].map((u) => (
          <label key={u} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="radio"
              name="units"
              value={u}
              checked={units === u}
              onChange={() => setUnits(u)}
              className="accent-accent"
            />
            <span
              className={`text-[12px] ${units === u ? "text-text-primary" : "text-text-muted"}`}
            >
              {u === "in" ? "Inches" : "Feet"}
            </span>
          </label>
        ))}
        {draft.require_unit_confirmation && (
          <span className="text-warning text-[11px] ml-auto">
            Large area detected - confirm units
          </span>
        )}
      </div>

      {/* Layer mapping */}
      <div className="flex-1 overflow-auto">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-text-muted text-left text-[10px] uppercase tracking-wider">
              <th className="pb-2 pr-3 w-32">Layer</th>
              <th className="pb-2 pr-3 w-48">Entities</th>
              {ROLES.map((r) => (
                <th key={r.key} className="pb-2 px-2 text-center w-24">
                  {r.label}
                  {r.required && <span className="text-accent ml-0.5">*</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {draft.layers.map((layer) => {
              const counts = draft.layer_counts[layer];
              return (
                <tr
                  key={layer}
                  className="border-t border-border-subtle hover:bg-bg-surface/50"
                >
                  <td className="py-1.5 pr-3 text-text-primary truncate max-w-[200px]">
                    {layer}
                  </td>
                  <td className="py-1.5 pr-3 text-text-muted">
                    {counts ? entitySummary(counts) : "-"}
                  </td>
                  {ROLES.map((role) => (
                    <td key={role.key} className="py-1.5 px-2 text-center">
                      <input
                        type="checkbox"
                        checked={mapping[role.key].includes(layer)}
                        onChange={() => toggleLayer(role.key, layer)}
                        className="accent-accent cursor-pointer"
                      />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {error && (
        <div className="text-error text-[12px] bg-error/10 border border-error/20 px-3 py-2">
          {error}
        </div>
      )}

      {/* Submit */}
      <div className="flex justify-end gap-3 pt-2">
        <button
          onClick={() => router.push("/")}
          className="px-4 py-1.5 text-[12px] border border-border-panel text-text-secondary hover:text-text-primary transition-colors"
        >
          Back
        </button>
        <button
          onClick={handleSubmit}
          disabled={loading}
          className="px-6 py-1.5 text-[12px] bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-40"
        >
          {loading ? "Loading..." : "Compute Tributary Areas"}
        </button>
      </div>
    </div>
  );
}
