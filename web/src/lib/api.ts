import type {
  DraftData,
  LayerMapping,
  ProcessResult,
  ProjectRecord,
  ProjectSummary,
  ViewMode,
} from "./types";

const BASE = "";
export type DemoId =
  | "default"
  | "geom_clean_1"
  | "fulton_356"
  | "franklin_246"
  | "manhattan_1300";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const contentType = res.headers.get("content-type") ?? "";
    let detail = res.statusText || "request failed";
    if (!contentType.includes("text/html")) {
      const body = (await res.text()).trim();
      if (body) detail = body.length > 200 ? `${body.slice(0, 200)}…` : body;
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export async function uploadDxf(file: File): Promise<DraftData> {
  const form = new FormData();
  form.append("upload", file);
  return request<DraftData>("/api/upload", { method: "POST", body: form });
}

export async function uploadDemo(demoId: DemoId = "default"): Promise<DraftData> {
  return request<DraftData>(`/api/upload?demo_id=${encodeURIComponent(demoId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ demo: true, demo_id: demoId }),
  });
}

export async function processJob(
  blobUrl: string,
  sourceUnits: string,
  layerMapping: LayerMapping,
): Promise<ProcessResult> {
  return request<ProcessResult>("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      blob_url: blobUrl,
      source_units: sourceUnits,
      layer_mapping: layerMapping,
    }),
  });
}

export async function listProjects(): Promise<ProjectSummary[]> {
  const data = await request<{ projects: ProjectSummary[] }>("/api/projects");
  return data.projects;
}

export interface SaveProjectInput {
  name: string;
  dxf_blob_url: string;
  dxf_filename: string;
  dxf_size_bytes: number | null;
  source_units: string;
  layer_mapping: LayerMapping;
  result: ProcessResult;
  view_mode: ViewMode;
}

export async function saveProject(input: SaveProjectInput): Promise<{ id: string }> {
  return request<{ id: string }>("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function getProject(id: string): Promise<ProjectRecord> {
  return request<ProjectRecord>(`/api/projects/${encodeURIComponent(id)}`);
}

export async function deleteProject(id: string): Promise<void> {
  const res = await fetch(`/api/projects/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }
}
