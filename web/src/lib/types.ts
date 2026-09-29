// --- Draft / Upload ---

export interface LayerCounts {
  [layerName: string]: { [entityType: string]: number };
}

export interface DraftData {
  id: string;
  filename: string;
  layers: string[];
  layer_counts: LayerCounts;
  suggestions: { [role: string]: string[] };
  suggestion_source?: "ai" | "heuristic";
  inferred_floor_area_sf: number;
  require_unit_confirmation: boolean;
  blob_url: string;
}

export interface LayerMapping {
  boundary: string[];
  additional_load: string[];
  opening: string[];
  wall: string[];
  beam: string[];
  support_point: string[];
  column_label: string[];
  floor_label: string[];
  datum: string[];
}

export type ViewMode = "plan" | "iso";

// --- Saved projects ---

export interface ProjectSummary {
  id: string;
  name: string;
  dxf_filename: string;
  dxf_size_bytes: number | null;
  updated_at: string;
}

export interface ProjectRecord extends ProjectSummary {
  dxf_blob_url: string;
  source_units: string;
  layer_mapping: LayerMapping;
  result: ProcessResult;
  view_mode: ViewMode;
}

// --- Process Result ---

export interface ProcessResult {
  status: "completed" | "failed" | "needs_review";
  geometry: GeometryPayload | null;
  artifacts: { dxf_url: string | null; xlsx_url: string | null };
  logs: string[];
  warnings: string[];
  error_message?: string;
}

// --- Geometry (canvas rendering) ---

export interface GeoJsonPolygon {
  type: "Polygon";
  coordinates: number[][][];
}

export interface GeoJsonMultiPolygon {
  type: "MultiPolygon";
  coordinates: number[][][][];
}

export interface GeoJsonLineString {
  type: "LineString";
  coordinates: number[][];
}

export interface GeoJsonPoint {
  type: "Point";
  coordinates: number[];
}

export type GeoJsonGeometry =
  | GeoJsonPolygon
  | GeoJsonMultiPolygon
  | GeoJsonLineString
  | GeoJsonPoint;

export interface ColumnData {
  index: number;
  label: string;
  point: [number, number];
  footprint: GeoJsonPolygon | GeoJsonMultiPolygon | null;
  tributary_region: GeoJsonPolygon | GeoJsonMultiPolygon | null;
  area_sf: number;
  area_sf_ceil: number;
  load_areas: LoadAreaData[];
  facade_length_ft: number;
  ends_here: boolean;
}

export interface WallData {
  wall_index: number;
  wall_line: GeoJsonLineString | null;
  tributary_region: GeoJsonPolygon | GeoJsonMultiPolygon | null;
  area_sf: number;
  area_sf_ceil: number;
  load_areas: LoadAreaData[];
}

export interface BeamData {
  beam_index: number;
  beam_line: GeoJsonLineString | null;
  source_layer: string;
}

export interface LoadAreaData {
  layer: string;
  area_sf: number;
  area_sf_ceil: number;
}

export interface LoadZoneData {
  index: number;
  layer: string;
  boundary: GeoJsonPolygon | GeoJsonMultiPolygon | null;
  area_sf: number;
  area_sf_ceil: number;
}

export interface FacadeSegment {
  label: string;
  type: "column" | "wall";
  length_ft: number;
  polyline: number[][];
}

export type AlignmentDatumSource =
  | "user"
  | "stacked"
  | "ai"
  | "wall_centroid"
  | "slab_centroid"
  | "none";

export interface DraftingError {
  point: [number, number];
  bbox: [number, number, number, number];
  kind: string;
  source_layer: string;
  reason: string;
}

export interface FloorData {
  floor_id: string;
  floor_index: number;
  slab_boundary: GeoJsonPolygon | GeoJsonMultiPolygon | null;
  load_zones: LoadZoneData[];
  columns: ColumnData[];
  walls: WallData[];
  beams: BeamData[];
  facade_segments: FacadeSegment[];
  facade_perimeter_ft: number;
  alignment_datum: [number, number] | null;
  alignment_datum_source: AlignmentDatumSource;
  drafting_errors: DraftingError[];
}

export interface GeometryBounds {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface GeometryPayload {
  floors: FloorData[];
  bounds: GeometryBounds;
  floor_count: number;
}
