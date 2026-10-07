"use client";

import { useCallback, useMemo, useState } from "react";
import TributaryCanvas from "./canvas/TributaryCanvas";
import {
  expandedPhysicalFloorCount,
  expandFloorIdentifier,
  floorStackSortValue,
} from "@/lib/floors";
import type {
  ColumnData,
  FloorData,
  GeoJsonMultiPolygon,
  GeoJsonPolygon,
  GeometryPayload,
  ProcessResult,
  ViewMode,
  WallData,
} from "@/lib/types";

type DatumPoints = Record<string, [number, number]>;
type SelectedColumn = {
  floorId: string;
  displayFloorId: string;
  colIndex: number;
};
type SelectedWall = {
  floorId: string;
  displayFloorId: string;
  wallIndex: number;
};

interface ResultsViewProps {
  result: ProcessResult;
  geometry: GeometryPayload;
  initialViewMode?: ViewMode;
  /** Tributaries drawn from the start; defaults to on in plan view, off in ISO. */
  initialShowTributaries?: boolean;
}

export default function ResultsView({
  result,
  geometry,
  initialViewMode = "plan",
  initialShowTributaries,
}: ResultsViewProps) {
  const initialIsoMode = initialViewMode === "iso";
  const [visibleFloors, setVisibleFloors] = useState<Set<string>>(
    () => new Set(geometry.floors.map((f) => f.floor_id)),
  );
  const [selectedColumn, setSelectedColumn] =
    useState<SelectedColumn | null>(null);
  const [selectedWall, setSelectedWall] = useState<SelectedWall | null>(null);
  const [hoveredColumn, setHoveredColumn] =
    useState<SelectedColumn | null>(null);
  const [showTributaries, setShowTributaries] = useState(
    initialShowTributaries ?? !initialIsoMode,
  );
  const [highlightUnlabeled, setHighlightUnlabeled] = useState(false);
  const [showSlabDebug, setShowSlabDebug] = useState(false);
  const [showSlabs, setShowSlabs] = useState(true);
  const [showWalls, setShowWalls] = useState(!initialIsoMode);
  const [showColumns, setShowColumns] = useState(true);
  const [showBeams, setShowBeams] = useState(true);
  const [viewMode, setViewMode] = useState<ViewMode>(initialViewMode);
  const [datumPoints, setDatumPoints] = useState<DatumPoints>({});
  const [datumEditFloorId, setDatumEditFloorId] = useState<string | null>(null);

  const applyViewMode = useCallback((mode: ViewMode) => {
    setViewMode(mode);
    setShowTributaries(mode === "plan");
    setShowWalls(mode === "plan");
  }, []);
  const physicalFloorCount = useMemo(
    () => expandedPhysicalFloorCount(geometry.floors),
    [geometry.floors],
  );
  const floorPlanItems = useMemo(
    () =>
      geometry.floors
        .map((floor) => {
          const representedFloors = expandFloorIdentifier(floor.floor_id);
          const sortValues = representedFloors.map((floorId) =>
            floorStackSortValue(floor, floorId),
          );
          return {
            floor,
            representedFloors,
            bottomSortValue: Math.min(...sortValues),
            topSortValue: Math.max(...sortValues),
          };
        })
        .sort((a, b) => {
          const topDelta = b.topSortValue - a.topSortValue;
          if (topDelta !== 0) return topDelta;

          const bottomDelta = b.bottomSortValue - a.bottomSortValue;
          if (bottomDelta !== 0) return bottomDelta;

          const indexDelta = b.floor.floor_index - a.floor.floor_index;
          if (indexDelta !== 0) return indexDelta;

          return b.floor.floor_id.localeCompare(a.floor.floor_id, undefined, {
            numeric: true,
          });
        }),
    [geometry.floors],
  );

  const toggleFloor = useCallback((floorId: string) => {
    setVisibleFloors((prev) => {
      const next = new Set(prev);
      if (next.has(floorId)) {
        next.delete(floorId);
      } else {
        next.add(floorId);
      }
      return next;
    });
  }, []);

  const handleStartDatumPick = useCallback((floorId: string) => {
    applyViewMode("plan");
    setVisibleFloors((prev) => {
      if (prev.has(floorId)) return prev;
      const next = new Set(prev);
      next.add(floorId);
      return next;
    });
    setDatumEditFloorId((prev) => (prev === floorId ? null : floorId));
  }, [applyViewMode]);

  const handleSetDatum = useCallback((floorId: string, point: [number, number]) => {
    setDatumPoints((prev) => ({ ...prev, [floorId]: point }));
    setDatumEditFloorId(null);
  }, []);

  const handleClearDatums = useCallback(() => {
    setDatumPoints({});
    setDatumEditFloorId(null);
  }, []);

  const handleSelectColumn = useCallback(
    (floorId: string, displayFloorId: string, colIndex: number) => {
      setSelectedWall(null);
      setSelectedColumn((prev) =>
        prev?.floorId === floorId &&
        prev?.displayFloorId === displayFloorId &&
        prev?.colIndex === colIndex
          ? null
          : { floorId, displayFloorId, colIndex },
      );
    },
    [],
  );

  const handleSelectWall = useCallback(
    (floorId: string, displayFloorId: string, wallIndex: number) => {
      setSelectedColumn(null);
      setSelectedWall((prev) =>
        prev?.floorId === floorId &&
        prev?.displayFloorId === displayFloorId &&
        prev?.wallIndex === wallIndex
          ? null
          : { floorId, displayFloorId, wallIndex },
      );
    },
    [],
  );

  const handleHoverColumn = useCallback(
    (
      floorId: string | null,
      displayFloorId: string | null,
      colIndex: number | null,
    ) => {
      if (floorId === null || displayFloorId === null || colIndex === null) {
        setHoveredColumn(null);
      } else {
        setHoveredColumn({ floorId, displayFloorId, colIndex });
      }
    },
    [],
  );

  // Find selected item data
  const selectedData = useMemo(() => {
    if (selectedColumn) {
      const floor = geometry.floors.find(
        (f) => f.floor_id === selectedColumn.floorId,
      );
      const col = floor?.columns.find(
        (c) => c.index === selectedColumn.colIndex,
      );
      if (floor && col) {
        return {
          type: "column" as const,
          floor,
          displayFloorId: selectedColumn.displayFloorId,
          col,
        };
      }
    }
    if (selectedWall) {
      const floor = geometry.floors.find(
        (f) => f.floor_id === selectedWall.floorId,
      );
      const wall = floor?.walls.find(
        (w) => w.wall_index === selectedWall.wallIndex,
      );
      if (floor && wall) {
        return {
          type: "wall" as const,
          floor,
          displayFloorId: selectedWall.displayFloorId,
          wall,
        };
      }
    }
    return null;
  }, [selectedColumn, selectedWall, geometry]);

  const wallCount = geometry.floors.reduce(
    (n, f) => n + f.walls.length,
    0,
  );
  const beamCount = geometry.floors.reduce(
    (n, f) => n + (f.beams?.length ?? 0),
    0,
  );
  const unlabeledCount = geometry.floors.reduce(
    (n, f) =>
      n +
      f.columns.filter((column) => isUnlabeledColumn(column.label)).length,
    0,
  );
  const disconnectedSlabSummary = useMemo(
    () => summarizeDisconnectedSlabs(geometry.floors),
    [geometry.floors],
  );
  const datumCount = geometry.floors.filter(
    (floor) => datumPoints[floor.floor_id] || floor.alignment_datum,
  ).length;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Top bar */}
      <div className="h-9 flex items-center justify-between px-3 border-b border-border-panel bg-bg-surface">
        <div className="flex items-center gap-3 text-[11px]">
          <span className="text-text-muted">
            {physicalFloorCount} floor(s)
          </span>
          {physicalFloorCount !== geometry.floor_count && (
            <span className="text-text-muted">
              / {geometry.floor_count} plan(s)
            </span>
          )}
          <span className="text-text-muted">|</span>
          <span className="text-text-muted">
            {geometry.floors.reduce((n, f) => n + f.columns.length, 0)} columns
          </span>
          {wallCount > 0 && (
            <>
              <span className="text-text-muted">|</span>
              <span className="text-text-muted">
                {wallCount} walls
              </span>
            </>
          )}
          {beamCount > 0 && (
            <>
              <span className="text-text-muted">|</span>
              <span className="text-text-muted">
                {beamCount} beams
              </span>
            </>
          )}
          <span className="text-text-muted">|</span>
          <span className="text-text-muted">
            datum {datumCount}/{geometry.floor_count}
          </span>
          {disconnectedSlabSummary.floorCount > 0 && (
            <>
              <span className="text-text-muted">|</span>
              <span className="text-warning">
                {disconnectedSlabSummary.extraIslandCount} slab island(s)
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="h-6 px-2 flex items-center gap-1.5 border border-border-panel text-[10px] uppercase tracking-wider text-text-secondary hover:text-text-primary cursor-pointer">
            <input
              type="checkbox"
              checked={showTributaries}
              onChange={(event) => setShowTributaries(event.target.checked)}
              className="accent-accent"
            />
            <span>Tributaries</span>
          </label>
          <label className="h-6 px-2 flex items-center gap-1.5 border border-border-panel text-[10px] uppercase tracking-wider text-text-secondary hover:text-text-primary cursor-pointer">
            <input
              type="checkbox"
              checked={highlightUnlabeled}
              onChange={(event) => setHighlightUnlabeled(event.target.checked)}
              className="accent-warning"
            />
            <span>
              Unlabeled
              {unlabeledCount > 0 ? ` (${unlabeledCount})` : ""}
            </span>
          </label>
          <label className="h-6 px-2 flex items-center gap-1.5 border border-border-panel text-[10px] uppercase tracking-wider text-text-secondary hover:text-text-primary cursor-pointer">
            <input
              type="checkbox"
              checked={showSlabDebug}
              onChange={(event) => setShowSlabDebug(event.target.checked)}
              className="accent-warning"
            />
            <span>
              Slab Debug
              {disconnectedSlabSummary.extraIslandCount > 0
                ? ` (${disconnectedSlabSummary.extraIslandCount})`
                : ""}
            </span>
          </label>
          {[
            { label: "Slab Boundary", checked: showSlabs, set: setShowSlabs },
            { label: "Walls", checked: showWalls, set: setShowWalls },
            { label: "Columns", checked: showColumns, set: setShowColumns },
            { label: "Beams", checked: showBeams, set: setShowBeams },
          ].map((toggle) => (
            <label
              key={toggle.label}
              className="h-6 px-2 flex items-center gap-1.5 border border-border-panel text-[10px] uppercase tracking-wider text-text-secondary hover:text-text-primary cursor-pointer"
            >
              <input
                type="checkbox"
                checked={toggle.checked}
                onChange={(event) => toggle.set(event.target.checked)}
                className="accent-accent"
              />
              <span>{toggle.label}</span>
            </label>
          ))}
          <div className="h-6 flex border border-border-panel overflow-hidden">
            {[
              { value: "plan", label: "2D" },
              { value: "iso", label: "Iso" },
            ].map((mode) => (
              <button
                key={mode.value}
                type="button"
                onClick={() => {
                  applyViewMode(mode.value as ViewMode);
                  setDatumEditFloorId(null);
                }}
                className={`w-10 text-[10px] uppercase tracking-wider transition-colors ${
                  viewMode === mode.value
                    ? "bg-accent text-white"
                    : "text-text-muted hover:text-text-primary"
                }`}
              >
                {mode.label}
              </button>
            ))}
          </div>
          {datumCount > 0 && (
            <button
              type="button"
              onClick={handleClearDatums}
              className="h-6 px-2 text-[10px] uppercase tracking-wider border border-border-panel text-text-secondary hover:text-text-primary hover:border-text-muted transition-colors"
            >
              Clear Datums
            </button>
          )}
          {result.artifacts.dxf_url && (
            <a
              href={`/api/download?url=${encodeURIComponent(result.artifacts.dxf_url)}`}
              className="px-3 py-0.5 text-[10px] uppercase tracking-wider border border-border-panel text-text-secondary hover:text-text-primary hover:border-text-muted transition-colors"
            >
              DXF
            </a>
          )}
          {result.artifacts.xlsx_url && (
            <a
              href={`/api/download?url=${encodeURIComponent(result.artifacts.xlsx_url)}`}
              className="px-3 py-0.5 text-[10px] uppercase tracking-wider border border-border-panel text-text-secondary hover:text-text-primary hover:border-text-muted transition-colors"
            >
              XLSX
            </a>
          )}
        </div>
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* Floor selector (left) */}
        <div className="w-48 border-r border-border-panel bg-bg-surface p-2 flex flex-col gap-1 overflow-auto">
          <div className="text-[10px] uppercase tracking-wider text-text-muted px-1 pb-1">
            Floor Plans
          </div>
          {floorPlanItems.map(({ floor, representedFloors }) => {
            const active = visibleFloors.has(floor.floor_id);
            const hasDatum = Boolean(datumPoints[floor.floor_id] || floor.alignment_datum);
            const pickingDatum = datumEditFloorId === floor.floor_id;
            return (
              <div
                key={floor.floor_id}
                className={`flex items-stretch gap-1 transition-colors ${
                  active
                    ? "text-text-primary bg-bg-panel"
                    : "text-text-muted hover:text-text-secondary"
                }`}
              >
                <button
                  type="button"
                  onClick={() => toggleFloor(floor.floor_id)}
                  className="min-w-0 flex-1 text-left px-2 py-1 text-[11px]"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate">{floor.floor_id}</span>
                    <span className="text-[10px] text-text-muted shrink-0">
                      {representedFloors.length > 1
                        ? `${representedFloors.length}f `
                        : ""}
                      {floor.columns.length}c
                      {(floor.beams?.length ?? 0) > 0
                        ? ` ${floor.beams?.length ?? 0}b`
                        : ""}
                    </span>
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => handleStartDatumPick(floor.floor_id)}
                  className={`w-14 text-[9px] uppercase tracking-wider border-l border-border-subtle ${
                    pickingDatum
                      ? "bg-warning text-black"
                      : hasDatum
                        ? "text-warning hover:text-text-primary"
                        : "text-text-muted hover:text-text-primary"
                  }`}
                  title={`Set datum for ${floor.floor_id}`}
                >
                  {pickingDatum ? "Pick" : hasDatum ? "Datum" : "Set"}
                </button>
              </div>
            );
          })}
        </div>

        {/* Canvas (center) */}
        <TributaryCanvas
          floors={geometry.floors}
          bounds={geometry.bounds}
          visibleFloors={visibleFloors}
          selectedColumn={selectedColumn}
          selectedWall={selectedWall}
          hoveredColumn={hoveredColumn}
          showTributaries={showTributaries}
          highlightUnlabeled={highlightUnlabeled}
          showSlabDebug={showSlabDebug}
          showSlabs={showSlabs}
          showWalls={showWalls}
          showColumns={showColumns}
          showBeams={showBeams}
          viewMode={viewMode}
          datumPoints={datumPoints}
          datumEditFloorId={datumEditFloorId}
          onSelectColumn={handleSelectColumn}
          onHoverColumn={handleHoverColumn}
          onSelectWall={handleSelectWall}
          onSetDatum={handleSetDatum}
        />

        {/* Info panel (right) */}
        <div className="w-56 border-l border-border-panel bg-bg-surface p-3 overflow-auto">
          <div className="text-[10px] uppercase tracking-wider text-text-muted pb-2">
            Details
          </div>
          {selectedData ? (
            selectedData.type === "column" ? (
              <ColumnInfo
                col={selectedData.col}
                floorId={selectedData.displayFloorId}
              />
            ) : (
              <WallInfo
                wall={selectedData.wall}
                floorId={selectedData.displayFloorId}
              />
            )
          ) : (
            <div className="text-text-muted text-[11px] pt-4">
              Click a tributary region to see details.
            </div>
          )}

          {/* Summary table for selected floor */}
          {selectedData && (
            <div className="mt-4 pt-3 border-t border-border-subtle">
              <div className="text-[10px] uppercase tracking-wider text-text-muted pb-2">
                Floor Summary
              </div>
              <div className="space-y-1 text-[11px]">
                <div className="flex justify-between">
                  <span className="text-text-muted">Columns</span>
                  <span className="text-text-primary">
                    {selectedData.floor.columns.length}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Walls</span>
                  <span className="text-text-primary">
                    {selectedData.floor.walls.length}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Beams</span>
                  <span className="text-text-primary">
                    {selectedData.floor.beams?.length ?? 0}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Perimeter</span>
                  <span className="text-text-primary">
                    {selectedData.floor.facade_perimeter_ft} ft
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function isUnlabeledColumn(label: string): boolean {
  return label.toUpperCase().includes("UNLABELED");
}

function summarizeDisconnectedSlabs(floors: FloorData[]): {
  floorCount: number;
  extraIslandCount: number;
} {
  let floorCount = 0;
  let extraIslandCount = 0;

  for (const floor of floors) {
    const componentCount = slabComponentCount(floor.slab_boundary);
    if (componentCount <= 1) continue;
    floorCount += 1;
    extraIslandCount += componentCount - 1;
  }

  return { floorCount, extraIslandCount };
}

function slabComponentCount(
  geometry: GeoJsonPolygon | GeoJsonMultiPolygon | null,
): number {
  if (!geometry) return 0;
  if (geometry.type === "Polygon") return 1;
  if (geometry.type === "MultiPolygon") return geometry.coordinates.length;
  return 0;
}

function ColumnInfo({
  col,
  floorId,
}: {
  col: ColumnData;
  floorId: string;
}) {
  const rows = [
    { label: "Label", value: col.label },
    { label: "Floor", value: floorId },
    { label: "Support", value: col.footprint ? "Footprint" : "Point" },
    { label: "Area", value: `${col.area_sf_ceil} SF` },
    { label: "Area (exact)", value: `${col.area_sf} SF` },
    { label: "Facade", value: `${col.facade_length_ft} ft` },
    {
      label: "Position",
      value: `(${col.point[0].toFixed(1)}, ${col.point[1].toFixed(1)})`,
    },
  ];

  return (
    <div className="space-y-1.5">
      <div className="text-accent text-sm font-medium">{col.label}</div>
      {rows.map((r) => (
        <div key={r.label} className="flex justify-between text-[11px]">
          <span className="text-text-muted">{r.label}</span>
          <span className="text-text-primary">{r.value}</span>
        </div>
      ))}
      {(col.load_areas?.length ?? 0) > 1 && (
        <div className="pt-2 mt-2 border-t border-border-subtle">
          <div className="text-[10px] uppercase tracking-wider text-text-muted pb-1">
            Load Zones
          </div>
          {(col.load_areas ?? []).map((loadArea) => (
            <div
              key={loadArea.layer}
              className="flex justify-between gap-3 text-[11px]"
            >
              <span className="text-text-muted truncate">
                {loadArea.layer}
              </span>
              <span className="text-text-primary">
                {loadArea.area_sf_ceil} SF
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WallInfo({
  wall,
  floorId,
}: {
  wall: WallData;
  floorId: string;
}) {
  const rows = [
    { label: "Type", value: "Wall" },
    { label: "Index", value: `#${wall.wall_index}` },
    { label: "Floor", value: floorId },
    { label: "Area", value: `${wall.area_sf_ceil} SF` },
    { label: "Area (exact)", value: `${wall.area_sf} SF` },
  ];

  return (
    <div className="space-y-1.5">
      <div className="text-error/80 text-sm font-medium">
        Wall {wall.wall_index}
      </div>
      {rows.map((r) => (
        <div key={r.label} className="flex justify-between text-[11px]">
          <span className="text-text-muted">{r.label}</span>
          <span className="text-text-primary">{r.value}</span>
        </div>
      ))}
      {(wall.load_areas?.length ?? 0) > 1 && (
        <div className="pt-2 mt-2 border-t border-border-subtle">
          <div className="text-[10px] uppercase tracking-wider text-text-muted pb-1">
            Load Zones
          </div>
          {(wall.load_areas ?? []).map((loadArea) => (
            <div
              key={loadArea.layer}
              className="flex justify-between gap-3 text-[11px]"
            >
              <span className="text-text-muted truncate">
                {loadArea.layer}
              </span>
              <span className="text-text-primary">
                {loadArea.area_sf_ceil} SF
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
