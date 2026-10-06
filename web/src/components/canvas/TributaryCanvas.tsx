"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Stage, Layer, Group, Line, Circle, Text } from "react-konva";
import type Konva from "konva";
import {
  buildFloorInstances,
  expandFloorIdentifier,
  floorStackSortValue,
  type RenderFloorInstance,
} from "@/lib/floors";
import type {
  FloorData,
  GeoJsonMultiPolygon,
  GeoJsonPolygon,
  GeometryBounds,
} from "@/lib/types";
import {
  computeViewTransform,
  geometryToRings,
  regionColor,
  regionStrokeColor,
  ringCentroid,
  SLAB_STROKE,
  SELECTED_FILL,
  SELECTED_STROKE,
  COLUMN_POINT_COLOR,
  COLUMN_POINT_RADIUS,
} from "@/lib/geo";

type ViewMode = "plan" | "iso";
type DatumPoints = Record<string, [number, number]>;

const FLOOR_STACK_SPACING_FT = 10;
const ISO_DEFAULT_YAW = -Math.PI / 4;
const ISO_DEFAULT_PITCH = 35 * (Math.PI / 180);
const ISO_MIN_PITCH = 12 * (Math.PI / 180);
const ISO_MAX_PITCH = 72 * (Math.PI / 180);
const ISO_DRAG_ROTATION_SPEED = 0.008;
const ISO_DRAG_TILT_SPEED = 0.006;
const ISO_GESTURE_ROTATION_SPEED = Math.PI / 180;
const MIN_ALIGNMENT_LABELS = 3;
const MAX_ALIGNMENT_RESIDUAL_FT = 8;

interface TributaryCanvasProps {
  floors: FloorData[];
  bounds: GeometryBounds;
  visibleFloors: Set<string>;
  selectedColumn: {
    floorId: string;
    displayFloorId: string;
    colIndex: number;
  } | null;
  selectedWall: {
    floorId: string;
    displayFloorId: string;
    wallIndex: number;
  } | null;
  hoveredColumn: {
    floorId: string;
    displayFloorId: string;
    colIndex: number;
  } | null;
  showTributaries: boolean;
  highlightUnlabeled: boolean;
  showSlabDebug: boolean;
  showSlabs: boolean;
  showWalls: boolean;
  showColumns: boolean;
  showBeams: boolean;
  viewMode: ViewMode;
  datumPoints: DatumPoints;
  datumEditFloorId: string | null;
  onSelectColumn: (
    floorId: string,
    displayFloorId: string,
    colIndex: number,
  ) => void;
  onHoverColumn: (
    floorId: string | null,
    displayFloorId: string | null,
    colIndex: number | null,
  ) => void;
  onSelectWall: (
    floorId: string,
    displayFloorId: string,
    wallIndex: number,
  ) => void;
  onSetDatum: (floorId: string, point: [number, number]) => void;
}

export default function TributaryCanvas({
  floors,
  bounds,
  visibleFloors,
  selectedColumn,
  selectedWall,
  hoveredColumn,
  showTributaries,
  highlightUnlabeled,
  showSlabDebug,
  showSlabs,
  showWalls,
  showColumns,
  showBeams,
  viewMode,
  datumPoints,
  datumEditFloorId,
  onSelectColumn,
  onHoverColumn,
  onSelectWall,
  onSetDatum,
}: TributaryCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<Konva.Stage>(null);
  const [size, setSize] = useState({ width: 800, height: 600 });
  const [stageScale, setStageScale] = useState(1);
  const [isPanning, setIsPanning] = useState(false);
  const [isSpaceHeld, setIsSpaceHeld] = useState(false);
  const isSpaceHeldRef = useRef(false);
  const pointerInsideRef = useRef(false);
  const [isOrbiting, setIsOrbiting] = useState(false);
  const [isoOrbit, setIsoOrbit] = useState<IsoOrbit>({
    yaw: ISO_DEFAULT_YAW,
    pitch: ISO_DEFAULT_PITCH,
  });

  // Fit container
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      if (width > 0 && height > 0) {
        setSize({ width, height });
      }
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const floorBoundsByKey = useMemo(() => {
    return new Map(
      floors.map((floor) => [
        floorSourceKey(floor),
        computeFloorBounds(floor) ?? bounds,
      ]),
    );
  }, [bounds, floors]);

  const autoAlignmentOriginsByKey = useMemo(
    () => buildAutoAlignmentOrigins(floors, floorBoundsByKey, bounds),
    [bounds, floorBoundsByKey, floors],
  );

  const alignmentOriginsByKey = useMemo(() => {
    const origins = new Map(autoAlignmentOriginsByKey);
    for (const floor of floors) {
      if (floor.alignment_datum) {
        origins.set(floorSourceKey(floor), floor.alignment_datum);
      }
      const manualDatum = datumPoints[floor.floor_id];
      if (manualDatum) {
        origins.set(floorSourceKey(floor), manualDatum);
      }
    }
    return origins;
  }, [autoAlignmentOriginsByKey, datumPoints, floors]);

  const renderFloors = useMemo(() => {
    if (viewMode === "iso") {
      return buildFloorInstances(floors, FLOOR_STACK_SPACING_FT);
    }

    return [...floors]
      .sort((a, b) => a.floor_index - b.floor_index)
      .map((floor, index): RenderFloorInstance => ({
        floor,
        sourceFloorId: floor.floor_id,
        displayFloorId: floor.floor_id,
        instanceId: `plan:${floor.floor_index}:${floor.floor_id}`,
        stackIndex: index,
        floorElevation: 0,
        // Plan view doesn't expand grouped floor ids — show markers as
        // normal.
        isBottomOfGroup: true,
      }));
  }, [floors, viewMode]);

  const renderBounds = useMemo(
    () =>
      projectedBounds(
        bounds,
        renderFloors,
        floorBoundsByKey,
        viewMode,
        isoOrbit,
        alignmentOriginsByKey,
      ),
    [
      alignmentOriginsByKey,
      bounds,
      floorBoundsByKey,
      isoOrbit,
      renderFloors,
      viewMode,
    ],
  );
  const transform = computeViewTransform(renderBounds, size.width, size.height);

  const verticalConnections = useMemo<VerticalConnection[]>(() => {
    if (viewMode !== "iso") return [];

    const visibleStack = [...renderFloors]
      .filter((instance) => visibleFloors.has(instance.sourceFloorId))
      .sort((a, b) => a.stackIndex - b.stackIndex);

    const connections: VerticalConnection[] = [];

    for (let i = 0; i < visibleStack.length - 1; i += 1) {
      const lower = visibleStack[i];
      const upper = visibleStack[i + 1];

      const lowerOrigin = alignmentOriginsByKey.get(floorSourceKey(lower.floor));
      const upperOrigin = alignmentOriginsByKey.get(floorSourceKey(upper.floor));

      // Phantom extension: when the upper instance is the bottom of its
      // grouped-floor expansion (so its ends_here columns are real
      // transfer events), extend each such column visually down to this
      // lower instance. Translate the upper-floor column point into the
      // lower floor's coordinate system using the alignment-origin delta
      // so the phantom lands on the slab below, not in DXF model space.
      if (upper.isBottomOfGroup && upperOrigin && lowerOrigin) {
        const dx = lowerOrigin[0] - upperOrigin[0];
        const dy = lowerOrigin[1] - upperOrigin[1];
        for (const col of upper.floor.columns) {
          if (!col.ends_here) continue;
          connections.push({
            kind: "column-phantom",
            lowerInstance: lower,
            upperInstance: upper,
            lowerPoint: [col.point[0] + dx, col.point[1] + dy],
            upperPoint: col.point,
          });
        }
      }

      if (lower.floor === upper.floor) {
        // Grouped-floor expansion (same FloorData on both instances) —
        // every column is identical, just at a different elevation.
        // Skip the matching loop and emit one connection per column.
        for (const col of upper.floor.columns) {
          connections.push({
            kind: "column",
            lowerInstance: lower,
            upperInstance: upper,
            lowerPoint: col.point,
            upperPoint: col.point,
          });
        }
      } else {
        // Different FloorData — match by datum-translated centroid
        // proximity. Wider tolerance to absorb small alignment drift
        // between floor drawings; labels are ignored entirely.
        const COLUMN_MATCH_TOLERANCE_FT = 4.0;
        const usedLowerColumns = new Set<number>();
        for (const upperCol of upper.floor.columns) {
          const upperAligned = alignedPoint(upperCol.point, upperOrigin);
          let best: { col: typeof upperCol; dist: number } | null = null;
          for (const lowerCol of lower.floor.columns) {
            if (usedLowerColumns.has(lowerCol.index)) continue;
            const lowerAligned = alignedPoint(lowerCol.point, lowerOrigin);
            const d = Math.hypot(
              upperAligned[0] - lowerAligned[0],
              upperAligned[1] - lowerAligned[1],
            );
            if (d > COLUMN_MATCH_TOLERANCE_FT) continue;
            if (!best || d < best.dist) best = { col: lowerCol, dist: d };
          }
          if (!best) continue;
          usedLowerColumns.add(best.col.index);
          connections.push({
            kind: "column",
            lowerInstance: lower,
            upperInstance: upper,
            lowerPoint: best.col.point,
            upperPoint: upperCol.point,
          });
        }
      }

      for (const upperWall of upper.floor.walls) {
        const upperCoords = wallVertices(upperWall);
        if (upperCoords.length < 2) continue;

        const lowerCoords = upperCoords.map((point) =>
          translateBetweenOrigins(point as [number, number], upperOrigin, lowerOrigin),
        );

        if (lowerCoords.length >= 2) {
          connections.push({
            kind: "wall",
            lowerInstance: lower,
            upperInstance: upper,
            lowerCoords,
            upperCoords: upperCoords as [number, number][],
            closed: wallIsClosed(upperWall),
          });
        }
      }
    }

    return connections;
  }, [alignmentOriginsByKey, renderFloors, viewMode, visibleFloors]);

  // CAD-style navigation:
  // - Trackpad pinch (ctrlKey) = zoom at cursor
  // - Trackpad two-finger scroll = pan
  // - In iso, primary click-drag = orbit
  // - Middle/right/shift/alt drag = pan
  // - In plan, primary background drag = pan
  const isPanningRef = useRef(false);
  const panStartRef = useRef({ x: 0, y: 0 });
  const panDragDistanceRef = useRef(0);
  const isOrbitingRef = useRef(false);
  const orbitStartRef = useRef({ x: 0, y: 0 });
  const orbitDragDistanceRef = useRef(0);
  const suppressClickRef = useRef(false);
  const gestureRotationRef = useRef(0);

  const suppressNextClickAfterDrag = useCallback((distance: number) => {
    if (distance <= 4) return;
    suppressClickRef.current = true;
    window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
  }, []);

  const startPan = useCallback((clientX: number, clientY: number) => {
    isPanningRef.current = true;
    panDragDistanceRef.current = 0;
    setIsPanning(true);
    panStartRef.current = { x: clientX, y: clientY };
  }, []);

  const stopPan = useCallback(() => {
    if (!isPanningRef.current) return;
    isPanningRef.current = false;
    setIsPanning(false);
    suppressNextClickAfterDrag(panDragDistanceRef.current);
  }, [suppressNextClickAfterDrag]);

  const stopOrbit = useCallback(() => {
    if (!isOrbitingRef.current) return;
    isOrbitingRef.current = false;
    setIsOrbiting(false);
    suppressNextClickAfterDrag(orbitDragDistanceRef.current);
  }, [suppressNextClickAfterDrag]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || viewMode !== "iso") return;

    const handleGestureStart = (event: Event) => {
      const gesture = event as WebKitGestureEvent;
      gestureRotationRef.current = gesture.rotation ?? 0;
      event.preventDefault();
    };

    const handleGestureChange = (event: Event) => {
      const gesture = event as WebKitGestureEvent;
      const rotation = gesture.rotation ?? 0;
      const delta = rotation - gestureRotationRef.current;
      gestureRotationRef.current = rotation;

      if (Number.isFinite(delta) && Math.abs(delta) > 0.01) {
        setIsoOrbit((current) => ({
          ...current,
          yaw: normalizeAngle(current.yaw + delta * ISO_GESTURE_ROTATION_SPEED),
        }));
      }
      event.preventDefault();
    };

    el.addEventListener("gesturestart", handleGestureStart, { passive: false });
    el.addEventListener("gesturechange", handleGestureChange, {
      passive: false,
    });

    return () => {
      el.removeEventListener("gesturestart", handleGestureStart);
      el.removeEventListener("gesturechange", handleGestureChange);
    };
  }, [viewMode]);

  // Spacebar-hold pan mode + global mouseup to clear pan/orbit state
  // when the user releases outside the canvas.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      // Only activate when the cursor is over the canvas — otherwise
      // space still triggers checkboxes, buttons, scrolling, etc.
      if (!pointerInsideRef.current) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" || target?.isContentEditable) return;
      if (isSpaceHeldRef.current) return;
      e.preventDefault();
      isSpaceHeldRef.current = true;
      setIsSpaceHeld(true);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      isSpaceHeldRef.current = false;
      setIsSpaceHeld(false);
    };
    const onMouseUp = () => {
      if (isPanningRef.current) {
        isPanningRef.current = false;
        setIsPanning(false);
      }
      if (isOrbitingRef.current) {
        isOrbitingRef.current = false;
        setIsOrbiting(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, []);

  const handleWheel = useCallback((e: Konva.KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const stage = stageRef.current;
    if (!stage) return;

    const isZoomGesture = e.evt.ctrlKey || e.evt.metaKey;

    if (!isZoomGesture) {
      // Two-finger trackpad scroll = pan
      stage.position({
        x: stage.x() - e.evt.deltaX,
        y: stage.y() - e.evt.deltaY,
      });
    } else {
      // Scroll wheel or pinch = zoom at cursor
      const oldScale = stage.scaleX();
      const pointer = stage.getPointerPosition();
      if (!pointer) return;

      const zoomFactor = e.evt.ctrlKey
        ? 1 - e.evt.deltaY * 0.005      // trackpad pinch (fine)
        : 1 - e.evt.deltaY * 0.001;     // scroll wheel (coarser steps)
      const newScale = Math.max(0.05, Math.min(oldScale * zoomFactor, 100));

      const mousePointTo = {
        x: (pointer.x - stage.x()) / oldScale,
        y: (pointer.y - stage.y()) / oldScale,
      };

      stage.scale({ x: newScale, y: newScale });
      stage.position({
        x: pointer.x - mousePointTo.x * newScale,
        y: pointer.y - mousePointTo.y * newScale,
      });
      setStageScale(newScale);
    }
    stage.batchDraw();
  }, []);

  const handleMouseDown = useCallback((e: Konva.KonvaEventObject<MouseEvent>) => {
    if (datumEditFloorId && viewMode === "plan" && e.evt.button === 0) {
      e.evt.preventDefault();
      return;
    }

    if (shouldStartPan(e, viewMode, isSpaceHeldRef.current)) {
      e.evt.preventDefault();
      startPan(e.evt.clientX, e.evt.clientY);
      return;
    }

    if (viewMode === "iso" && e.evt.button === 0) {
      e.evt.preventDefault();
      isOrbitingRef.current = true;
      orbitDragDistanceRef.current = 0;
      setIsOrbiting(true);
      orbitStartRef.current = { x: e.evt.clientX, y: e.evt.clientY };
    }
  }, [datumEditFloorId, startPan, viewMode]);

  const handleMouseMove = useCallback((e: Konva.KonvaEventObject<MouseEvent>) => {
    if (isOrbitingRef.current) {
      const dx = e.evt.clientX - orbitStartRef.current.x;
      const dy = e.evt.clientY - orbitStartRef.current.y;
      orbitStartRef.current = { x: e.evt.clientX, y: e.evt.clientY };
      orbitDragDistanceRef.current += Math.abs(dx) + Math.abs(dy);

      if (Math.abs(dx) > 0 || Math.abs(dy) > 0) {
        setIsoOrbit((current) => ({
          yaw: normalizeAngle(current.yaw + dx * ISO_DRAG_ROTATION_SPEED),
          pitch: clamp(
            current.pitch - dy * ISO_DRAG_TILT_SPEED,
            ISO_MIN_PITCH,
            ISO_MAX_PITCH,
          ),
        }));
      }
      return;
    }

    if (!isPanningRef.current) return;
    const stage = stageRef.current;
    if (!stage) return;
    const dx = e.evt.clientX - panStartRef.current.x;
    const dy = e.evt.clientY - panStartRef.current.y;
    panStartRef.current = { x: e.evt.clientX, y: e.evt.clientY };
    panDragDistanceRef.current += Math.abs(dx) + Math.abs(dy);
    stage.position({ x: stage.x() + dx, y: stage.y() + dy });
    stage.batchDraw();
  }, []);

  const handleMouseUp = useCallback(() => {
    stopOrbit();
    stopPan();
  }, [stopOrbit, stopPan]);

  const handleMouseLeave = useCallback(() => {
    stopOrbit();
    stopPan();
  }, [stopOrbit, stopPan]);

  const handleCanvasClick = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      if (!datumEditFloorId || viewMode !== "plan") return;
      if (suppressClickRef.current) return;

      const stage = stageRef.current;
      const pointer = stage?.getPointerPosition();
      if (!stage || !pointer) return;

      e.cancelBubble = true;
      onSetDatum(
        datumEditFloorId,
        screenToPlanPoint(pointer, stage, transform),
      );
    },
    [datumEditFloorId, onSetDatum, transform, viewMode],
  );

  return (
    <div
      ref={containerRef}
      className="relative flex-1 bg-bg-base overflow-hidden"
      onMouseEnter={() => { pointerInsideRef.current = true; }}
      onMouseLeave={() => { pointerInsideRef.current = false; }}
    >
      {datumEditFloorId && (
        <div className="pointer-events-none absolute left-3 top-3 z-10 border border-warning/60 bg-bg-surface/95 px-2 py-1 text-[10px] uppercase tracking-wider text-warning">
          Pick datum: {datumEditFloorId}
        </div>
      )}
      <Stage
        ref={stageRef}
        width={size.width}
        height={size.height}
        onWheel={handleWheel}
        onClick={handleCanvasClick}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
        onContextMenu={(e) => e.evt.preventDefault()}
        style={{
          cursor: isPanning || isOrbiting
            ? "grabbing"
            : datumEditFloorId
              ? "crosshair"
              : isSpaceHeld
                ? "grab"
                : viewMode === "iso"
                  ? "grab"
                  : "default",
        }}
      >
        <Layer>
          {renderFloors.map((instance) => {
            const { floor } = instance;
            if (!visibleFloors.has(instance.sourceFloorId)) return null;
            const totalCols = floor.columns.length;
            const floorBounds =
              floorBoundsByKey.get(floorSourceKey(floor)) ?? bounds;
            const datumPoint = datumPoints[instance.sourceFloorId];
            const alignmentOrigin = alignmentOriginsByKey.get(
              floorSourceKey(floor),
            );
            const isDatumTarget = datumEditFloorId === instance.sourceFloorId;
            const projection = projectionForFloor(
              viewMode,
              floorBounds,
              instance.floorElevation,
              isoOrbit,
              alignmentOrigin,
            );
            // Inverse scale factor — keeps labels/dots at constant screen size
            const inv = 1 / stageScale;
            // Label visibility: hide when too zoomed out to be readable
            const showLabels = stageScale > 0.4;
            const showDetail = stageScale > 1.5;
            const slabComponents = geometryToPolygons(floor.slab_boundary);
            const mainSlabComponentIndex = largestPolygonIndex(slabComponents);

            return (
              <Group key={instance.instanceId}>
              {/* Slab boundary */}
              {showSlabs && floor.slab_boundary &&
                geometryToRings(floor.slab_boundary).map((ring, i) => (
                  <Line
                    key={`boundary-${i}`}
                    points={transformRing(ring, transform, projection)}
                    closed
                    stroke={SLAB_STROKE}
                    strokeWidth={1.5 * inv}
                    listening={false}
	                  />
	                ))}

              {showSlabDebug && slabComponents.length > 0 && (
                <Group key={`slab-debug-${instance.instanceId}`} listening={false}>
                  {slabComponents.map((polygon, polygonIndex) => {
                    const isIsland =
                      slabComponents.length > 1 &&
                      polygonIndex !== mainSlabComponentIndex;
                    const shell = polygon[0] ?? [];
                    const [labelX, labelY] = ringCentroid(shell);
                    const [tx, ty] = transformPoint(
                      labelX,
                      labelY,
                      transform,
                      projection,
                    );

                    return (
                      <Group
                        key={`slab-component-${polygonIndex}`}
                        listening={false}
                      >
                        {polygon.map((ring, ringIndex) => (
                          <Line
                            key={`slab-component-ring-${ringIndex}`}
                            points={transformRing(ring, transform, projection)}
                            closed
                            fill={
                              ringIndex === 0
                                ? isIsland
                                  ? "hsla(38, 92%, 50%, 0.22)"
                                  : "hsla(188, 95%, 45%, 0.08)"
                                : undefined
                            }
                            stroke={isIsland ? "#f59e0b" : "#22d3ee"}
                            strokeWidth={(isIsland ? 3 : 1.5) * inv}
                            dash={[10 * inv, 5 * inv]}
                          />
                        ))}
                        {isIsland && showLabels && (
                          <Text
                            x={tx + 6 * inv}
                            y={ty - 8 * inv}
                            text={`ISLAND ${polygonIndex + 1}`}
                            fontSize={10 * inv}
                            fontFamily="JetBrains Mono, monospace"
                            fontStyle="bold"
                            fill="#fbbf24"
                          />
                        )}
                      </Group>
                    );
                  })}
                </Group>
              )}

              {(floor.load_zones?.length ?? 0) > 1 &&
                floor.load_zones.map((zone) =>
                  geometryToRings(zone.boundary).map((ring, i) => (
                    <Line
                      key={`load-zone-${zone.index}-${i}`}
                      points={transformRing(ring, transform, projection)}
                      closed
                      fill={
                        i === 0
                          ? regionColor(zone.index, floor.load_zones.length, 0.06)
                          : undefined
                      }
                      stroke={regionStrokeColor(
                        zone.index,
                        floor.load_zones.length,
                      )}
                      strokeWidth={1 * inv}
                      dash={[8 * inv, 5 * inv]}
                      listening={false}
                    />
                  )),
                )}

              {/* Wall tributary regions — hidden; wall lines show location,
                  column regions fill the slab. Area data still in exports. */}

              {/* Column tributary regions */}
              {showTributaries && floor.columns.map((col) => {
                if (!col.tributary_region) return null;
                const isSelected =
                  selectedColumn?.floorId === instance.sourceFloorId &&
                  selectedColumn?.displayFloorId === instance.displayFloorId &&
                  selectedColumn?.colIndex === col.index;
                const isHovered =
                  hoveredColumn?.floorId === instance.sourceFloorId &&
                  hoveredColumn?.displayFloorId === instance.displayFloorId &&
                  hoveredColumn?.colIndex === col.index;
                const rings = geometryToRings(col.tributary_region);
                const fill = isSelected
                  ? SELECTED_FILL
                  : regionColor(
                      col.index,
                      totalCols,
                      isHovered ? 0.3 : 0.2,
                    );
                const stroke = isSelected
                  ? SELECTED_STROKE
                  : regionStrokeColor(col.index, totalCols);

                return rings.map((ring, i) => (
                  <Line
                    key={`col-${col.index}-${i}`}
                    points={transformRing(ring, transform, projection)}
                    closed
                    fill={i === 0 ? fill : undefined}
                    stroke={stroke}
                    strokeWidth={(isSelected ? 1.5 : 0.5) * inv}
                    onClick={() => {
                      if (datumEditFloorId) return;
                      if (suppressClickRef.current) return;
                      onSelectColumn(
                        instance.sourceFloorId,
                        instance.displayFloorId,
                        col.index,
                      );
                    }}
                    onMouseEnter={() =>
                      onHoverColumn(
                        instance.sourceFloorId,
                        instance.displayFloorId,
                        col.index,
                      )
                    }
                    onMouseLeave={() => onHoverColumn(null, null, null)}
                  />
                ));
              })}

              {/* Column supports: closed footprints when available, point markers otherwise. */}
              {showColumns && floor.columns.map((col) => {
                const isSelected =
                  selectedColumn?.floorId === instance.sourceFloorId &&
                  selectedColumn?.displayFloorId === instance.displayFloorId &&
                  selectedColumn?.colIndex === col.index;
                const isHovered =
                  hoveredColumn?.floorId === instance.sourceFloorId &&
                  hoveredColumn?.displayFloorId === instance.displayFloorId &&
                  hoveredColumn?.colIndex === col.index;
                const footprintRings = geometryToRings(col.footprint);
                if (footprintRings.length > 0) {
                  return footprintRings.map((ring, i) => (
                    <Line
                      key={`footprint-${col.index}-${i}`}
                      points={transformRing(ring, transform, projection)}
                      closed
                      fill={
                        i === 0
                          ? isSelected
                            ? SELECTED_FILL
                            : isHovered
                              ? "hsla(217, 91%, 60%, 0.22)"
                              : "hsla(217, 91%, 60%, 0.14)"
                          : undefined
                      }
                      stroke={
                        isSelected
                          ? SELECTED_STROKE
                          : COLUMN_POINT_COLOR
                      }
                      strokeWidth={(isSelected ? 2 : 1.25) * inv}
                      onClick={() => {
                        if (datumEditFloorId) return;
                        if (suppressClickRef.current) return;
                        onSelectColumn(
                          instance.sourceFloorId,
                          instance.displayFloorId,
                          col.index,
                        );
                      }}
                      onMouseEnter={() =>
                        onHoverColumn(
                          instance.sourceFloorId,
                          instance.displayFloorId,
                          col.index,
                        )
                      }
                      onMouseLeave={() => onHoverColumn(null, null, null)}
                    />
                  ));
                }

                const [cx, cy] = transformPoint(
                  col.point[0],
                  col.point[1],
                  transform,
                  projection,
                );
                return (
                  <Circle
                    key={`pt-${col.index}`}
                    x={cx}
                    y={cy}
                    radius={
                      (isSelected
                        ? COLUMN_POINT_RADIUS + 1.5
                        : COLUMN_POINT_RADIUS) * inv
                    }
                    fill={COLUMN_POINT_COLOR}
                    stroke={
                      isSelected
                        ? SELECTED_STROKE
                        : undefined
                    }
                    strokeWidth={isSelected ? 1.5 * inv : 0}
                    onClick={() => {
                      if (datumEditFloorId) return;
                      if (suppressClickRef.current) return;
                      onSelectColumn(
                        instance.sourceFloorId,
                        instance.displayFloorId,
                        col.index,
                      );
                    }}
                    onMouseEnter={() =>
                      onHoverColumn(
                        instance.sourceFloorId,
                        instance.displayFloorId,
                        col.index,
                      )
                    }
                    onMouseLeave={() => onHoverColumn(null, null, null)}
                  />
                );
              })}

              {/* Alignment datum overlay — cyan crosshair + source chip
                  so the user can visually verify (and later override) the
                  point being used to align this floor against the others. */}
              {floor.alignment_datum && (() => {
                const [dx, dy] = transformPoint(
                  floor.alignment_datum[0],
                  floor.alignment_datum[1],
                  transform,
                  projection,
                );
                const armLen = 14 * inv;
                const ringRadius = 6 * inv;
                const dotRadius = 1.75 * inv;
                const source = floor.alignment_datum_source;
                const sourceLabel =
                  source === "user"
                    ? "USER"
                    : source === "stacked"
                      ? "STACK"
                      : source === "ai"
                        ? "AI"
                        : source === "wall_centroid"
                          ? "WALL"
                          : source === "slab_centroid"
                            ? "SLAB"
                            : "—";
                return (
                  <Group key={`datum-${floor.floor_index}`} listening={false}>
                    <Line
                      points={[dx - armLen, dy, dx + armLen, dy]}
                      stroke="#22d3ee"
                      strokeWidth={1.25 * inv}
                    />
                    <Line
                      points={[dx, dy - armLen, dx, dy + armLen]}
                      stroke="#22d3ee"
                      strokeWidth={1.25 * inv}
                    />
                    <Circle
                      x={dx}
                      y={dy}
                      radius={ringRadius}
                      stroke="#22d3ee"
                      strokeWidth={1.25 * inv}
                    />
                    <Circle x={dx} y={dy} radius={dotRadius} fill="#22d3ee" />
                    {viewMode !== "iso" && (
                      <Text
                        x={dx + 10 * inv}
                        y={dy - 12 * inv}
                        text={`DATUM·${sourceLabel}`}
                        fontSize={8 * inv}
                        fontFamily="JetBrains Mono, monospace"
                        fill="#22d3ee"
                      />
                    )}
                  </Group>
                );
              })()}

              {/* Drafting errors: yellow X over column-layer geometry that
                  didn't qualify as a closed-polygon footprint. Helps spot
                  open polylines, single-line "columns", etc. */}
              {floor.drafting_errors?.map((err, i) => {
                const [bx0, by0] = transformPoint(err.bbox[0], err.bbox[1], transform, projection);
                const [bx1, by1] = transformPoint(err.bbox[2], err.bbox[3], transform, projection);
                const [cx, cy] = transformPoint(err.point[0], err.point[1], transform, projection);
                const half = Math.max(Math.abs(bx1 - bx0), Math.abs(by1 - by0)) / 2 + 4 * inv;
                return (
                  <Group key={`derr-${i}`} listening={false}>
                    <Line
                      points={[cx - half, cy - half, cx + half, cy + half]}
                      stroke="#facc15"
                      strokeWidth={1.5 * inv}
                    />
                    <Line
                      points={[cx - half, cy + half, cx + half, cy - half]}
                      stroke="#facc15"
                      strokeWidth={1.5 * inv}
                    />
                    {viewMode !== "iso" && (
                      <Text
                        x={cx + half + 4 * inv}
                        y={cy - 5 * inv}
                        text={`DRAFT·${err.reason.toUpperCase()}`}
                        fontSize={8 * inv}
                        fontFamily="JetBrains Mono, monospace"
                        fill="#facc15"
                      />
                    )}
                  </Group>
                );
              })}

              {/* Transfer scaffolding: a column that ends on the floor
                  ABOVE this instance gets visually extended down by one
                  floor — the red ring + dot lands on the slab below the
                  transfer (i.e. on THIS instance). Upper-floor column
                  points are translated into this floor's coordinate
                  system via the alignment-origin delta. */}
              {(() => {
                const upper = renderFloors.find(
                  (i) =>
                    i.stackIndex === instance.stackIndex + 1 &&
                    visibleFloors.has(i.sourceFloorId),
                );
                if (!upper || !upper.isBottomOfGroup) return null;
                const lowerOrigin = alignmentOriginsByKey.get(floorSourceKey(floor));
                const upperOrigin = alignmentOriginsByKey.get(floorSourceKey(upper.floor));
                if (!lowerOrigin || !upperOrigin) return null;
                const dxAlign = lowerOrigin[0] - upperOrigin[0];
                const dyAlign = lowerOrigin[1] - upperOrigin[1];
                return upper.floor.columns.map((col) => {
                  if (!col.ends_here) return null;
                  const [cx, cy] = transformPoint(
                    col.point[0] + dxAlign,
                    col.point[1] + dyAlign,
                    transform,
                    projection,
                  );
                  return (
                    <Group key={`ends-${upper.instanceId}-${col.index}`} listening={false}>
                      <Circle
                        x={cx}
                        y={cy}
                        radius={(COLUMN_POINT_RADIUS + 4) * inv}
                        stroke="#ef4444"
                        strokeWidth={1.75 * inv}
                      />
                      <Circle
                        x={cx}
                        y={cy}
                        radius={(COLUMN_POINT_RADIUS - 0.5) * inv}
                        fill="#ef4444"
                      />
                    </Group>
                  );
                });
              })()}

              {/* Column labels — anchored to column point, offset right.
                  Show labels even on zero-tributary columns (rescued orphans
                  outside the slab) so the user can still ID them. */}
              {showLabels && viewMode !== "iso" &&
                floor.columns.map((col) => {
                  const [px, py] = transformPoint(
                    col.point[0],
                    col.point[1],
                    transform,
                    projection,
                  );
                  const hasArea = col.area_sf_ceil > 0;
                  const label = showTributaries && showDetail && hasArea
                    ? `${col.label}\n${col.area_sf_ceil} SF`
                    : col.label || (hasArea ? `${col.area_sf_ceil}` : "");
                  if (!label) return null;
                  return (
                    <Text
                      key={`label-${col.index}`}
                      x={px + 6 * inv}
                      y={py - (showDetail ? 6 : 4) * inv}
                      text={label}
                      fontSize={9 * inv}
                      fontFamily="JetBrains Mono, monospace"
                      fill="#e5e5e5"
                      listening={false}
                    />
                  );
                })}

              {/* Unlabeled debug overlay: additive only, so toggling it does not restyle the full scene. */}
              {highlightUnlabeled &&
                floor.columns
                  .filter((col) => isUnlabeledColumn(col.label))
                  .map((col) => {
                    const [px, py] = transformPoint(
                      col.point[0],
                      col.point[1],
                      transform,
                      projection,
                    );
                    const regionRings =
                      showTributaries && col.tributary_region
                        ? geometryToRings(col.tributary_region)
                        : [];
                    const footprintRings = geometryToRings(col.footprint);
                    const label =
                      col.area_sf_ceil > 0
                        ? `${col.label}\n${col.area_sf_ceil} SF`
                        : col.label;

                    return (
                      <Group
                        key={`unlabeled-debug-${col.index}`}
                        listening={false}
                      >
                        {regionRings.map((ring, i) => (
                          <Line
                            key={`unlabeled-region-${i}`}
                            points={transformRing(ring, transform, projection)}
                            closed
                            fill={
                              i === 0
                                ? "hsla(48, 96%, 53%, 0.18)"
                                : undefined
                            }
                            stroke="#facc15"
                            strokeWidth={2.5 * inv}
                            dash={[8 * inv, 4 * inv]}
                          />
                        ))}
                        {footprintRings.length > 0 ? (
                          footprintRings.map((ring, i) => (
                            <Line
                              key={`unlabeled-footprint-${i}`}
                              points={transformRing(ring, transform, projection)}
                              closed
                              fill={
                                i === 0
                                  ? "hsla(48, 96%, 53%, 0.34)"
                                  : undefined
                              }
                              stroke="#fef08a"
                              strokeWidth={3 * inv}
                            />
                          ))
                        ) : (
                          <Circle
                            x={px}
                            y={py}
                            radius={(COLUMN_POINT_RADIUS + 2) * inv}
                            fill="#facc15"
                            stroke="#fef08a"
                            strokeWidth={1.5 * inv}
                          />
                        )}
                        <Text
                          x={px + 8 * inv}
                          y={py - 10 * inv}
                          text={label}
                          fontSize={10 * inv}
                          fontFamily="JetBrains Mono, monospace"
                          fontStyle="bold"
                          fill="#fef08a"
                        />
                      </Group>
                    );
                  })}

              {/* Beam transfer linework: display only; not part of tributary solve yet. */}
              {showBeams && floor.beams?.map((beam) => {
                if (!beam.beam_line) return null;
                return (
                  <Line
                    key={`beamline-${beam.beam_index}`}
                    points={transformRing(
                      beam.beam_line.coordinates,
                      transform,
                      projection,
                    )}
                    stroke="#f59e0b"
                    strokeWidth={3 * inv}
                    dash={[10 * inv, 4 * inv]}
                    lineCap="round"
                    lineJoin="round"
                    listening={false}
                  />
                );
              })}

              {/* Wall linework */}
              {showWalls && floor.walls.map((wall) => {
                if (!wall.wall_line) return null;
                const coords = wall.wall_line.coordinates;
                const isClosed = coords.length >= 4;
                const isSelected =
                  selectedWall?.floorId === instance.sourceFloorId &&
                  selectedWall?.displayFloorId === instance.displayFloorId &&
                  selectedWall?.wallIndex === wall.wall_index;
                return (
                  <Line
                    key={`wallline-${wall.wall_index}`}
                    points={transformRing(coords, transform, projection)}
                    closed={isClosed}
                    fill={
                      isClosed
                        ? "hsla(0, 70%, 45%, 0.35)"
                        : undefined
                    }
                    stroke={
                      isSelected
                        ? SELECTED_STROKE
                        : "#ef4444"
                    }
                    strokeWidth={(isSelected ? 4 : isClosed ? 1.5 : 3) * inv}
                    lineCap="square"
                    onClick={() => {
                      if (datumEditFloorId) return;
                      if (suppressClickRef.current) return;
                      onSelectWall(
                        instance.sourceFloorId,
                        instance.displayFloorId,
                        wall.wall_index,
                      );
                    }}
                  />
                );
              })}

              {/* Floor ID label — anchored to slab boundary centroid */}
              {floor.slab_boundary && (() => {
                const rings = geometryToRings(floor.slab_boundary);
                if (!rings.length || !rings[0].length) return null;
                const [cx, cy] = ringCentroid(rings[0]);
                const [tx, ty] = transformPoint(cx, cy, transform, projection);
                return (
                  <Text
                    key={`floor-label-${instance.displayFloorId}`}
                    x={tx}
                    y={ty}
                    text={instance.displayFloorId}
                    fontSize={16 * inv}
                    fontFamily="JetBrains Mono, monospace"
                    fontStyle="bold"
                    fill="#3b82f6"
                    opacity={0.5}
                    align="center"
                    offsetX={40 * inv}
                    offsetY={8 * inv}
                    listening={false}
                  />
                );
              })()}

              {datumPoint && (() => {
                const [dx, dy] = transformPoint(
                  datumPoint[0],
                  datumPoint[1],
                  transform,
                  projection,
                );
                const markerSize = (isDatumTarget ? 9 : 7) * inv;
                return (
                  <Group key={`datum-${instance.instanceId}`} listening={false}>
                    <Line
                      points={[dx - markerSize, dy, dx + markerSize, dy]}
                      stroke="#facc15"
                      strokeWidth={(isDatumTarget ? 2.25 : 1.75) * inv}
                      lineCap="round"
                    />
                    <Line
                      points={[dx, dy - markerSize, dx, dy + markerSize]}
                      stroke="#facc15"
                      strokeWidth={(isDatumTarget ? 2.25 : 1.75) * inv}
                      lineCap="round"
                    />
                    <Circle
                      x={dx}
                      y={dy}
                      radius={(isDatumTarget ? 4.5 : 3.5) * inv}
                      fill="hsla(48, 96%, 53%, 0.28)"
                      stroke="#fef08a"
                      strokeWidth={1.25 * inv}
                    />
                    {showLabels && (
                      <Text
                        x={dx + 8 * inv}
                        y={dy + 4 * inv}
                        text="DATUM"
                        fontSize={8 * inv}
                        fontFamily="JetBrains Mono, monospace"
                        fill="#fef08a"
                      />
                    )}
                  </Group>
                );
              })()}
              </Group>
            );
          })}

          {viewMode === "iso" &&
            verticalConnections
              .filter((conn) =>
                conn.kind === "wall"
                  ? showWalls
                  : showColumns
              )
              .map((conn, idx) => {
              const lowerProjection = projectionForFloor(
                "iso",
                floorBoundsByKey.get(floorSourceKey(conn.lowerInstance.floor)) ?? bounds,
                conn.lowerInstance.floorElevation,
                isoOrbit,
                alignmentOriginsByKey.get(floorSourceKey(conn.lowerInstance.floor)),
              );
              const upperProjection = projectionForFloor(
                "iso",
                floorBoundsByKey.get(floorSourceKey(conn.upperInstance.floor)) ?? bounds,
                conn.upperInstance.floorElevation,
                isoOrbit,
                alignmentOriginsByKey.get(floorSourceKey(conn.upperInstance.floor)),
              );
              const inv = 1 / stageScale;

              if (conn.kind === "column" || conn.kind === "column-phantom") {
                const [lx, ly] = transformPoint(
                  conn.lowerPoint[0],
                  conn.lowerPoint[1],
                  transform,
                  lowerProjection,
                );
                const [ux, uy] = transformPoint(
                  conn.upperPoint[0],
                  conn.upperPoint[1],
                  transform,
                  upperProjection,
                );
                const phantom = conn.kind === "column-phantom";
                return (
                  <Line
                    key={`vconn-${idx}`}
                    points={[lx, ly, ux, uy]}
                    stroke={COLUMN_POINT_COLOR}
                    strokeWidth={(phantom ? 1.0 : 1.5) * inv}
                    opacity={phantom ? 0.45 : 0.85}
                    dash={phantom ? [6 * inv, 4 * inv] : undefined}
                    lineCap="round"
                    listening={false}
                  />
                );
              }

              // Wall: build translucent quad faces between consecutive
              // matched vertex pairs to give the wall visible thickness
              // in iso. 20% fill opacity, faint outline.
              const lowerScreen = conn.lowerCoords.map(
                (p) => transformPoint(p[0], p[1], transform, lowerProjection),
              );
              const upperScreen = conn.upperCoords.map(
                (p) => transformPoint(p[0], p[1], transform, upperProjection),
              );
              return (
                <Group key={`vconn-${idx}`} listening={false}>
                  {Array.from({
                    length: conn.closed
                      ? lowerScreen.length
                      : Math.max(0, lowerScreen.length - 1),
                  }).map((_, i) => {
                    const nextIndex = (i + 1) % lowerScreen.length;
                    const l1 = lowerScreen[i];
                    const l2 = lowerScreen[nextIndex];
                    const u2 = upperScreen[nextIndex];
                    const u1 = upperScreen[i];
                    return (
                      <Line
                        key={`wface-${i}`}
                        points={[l1[0], l1[1], l2[0], l2[1], u2[0], u2[1], u1[0], u1[1]]}
                        closed
                        fill="hsla(0, 70%, 45%, 0.2)"
                        stroke="hsla(0, 70%, 45%, 0.5)"
                        strokeWidth={0.5 * inv}
                      />
                    );
                  })}
                </Group>
              );
            })}
        </Layer>
      </Stage>
    </div>
  );
}

type Transform = ReturnType<typeof computeViewTransform>;
type Projection = {
  viewMode: ViewMode;
  floorElevation: number;
  originX: number;
  originY: number;
  orbit: IsoOrbit;
};
type IsoOrbit = {
  yaw: number;
  pitch: number;
};
type WebKitGestureEvent = Event & { rotation?: number };

function transformPoint(
  x: number,
  y: number,
  t: Transform,
  projection: Projection,
): [number, number] {
  const [projectedX, projectedY] = projectPoint(x, y, projection);
  return [
    projectedX * t.scaleX + t.offsetX,
    projectedY * t.scaleY + t.offsetY,
  ];
}

function transformRing(
  ring: number[][],
  t: Transform,
  projection: Projection,
): number[] {
  const flat: number[] = [];
  for (const [x, y] of ring) {
    const [screenX, screenY] = transformPoint(x, y, t, projection);
    flat.push(screenX, screenY);
  }
  return flat;
}

function geometryToPolygons(
  geom: GeoJsonPolygon | GeoJsonMultiPolygon | null,
): number[][][][] {
  if (!geom) return [];
  if (geom.type === "Polygon") return [geom.coordinates];
  if (geom.type === "MultiPolygon") return geom.coordinates;
  return [];
}

function largestPolygonIndex(polygons: number[][][][]): number {
  let largestIndex = -1;
  let largestArea = 0;

  polygons.forEach((polygon, index) => {
    const area = Math.abs(ringArea(polygon[0] ?? []));
    if (area > largestArea) {
      largestArea = area;
      largestIndex = index;
    }
  });

  return largestIndex;
}

function ringArea(ring: number[][]): number {
  let area = 0;
  for (let i = 0; i < ring.length; i += 1) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % ring.length];
    area += x1 * y2 - x2 * y1;
  }
  return area / 2;
}

function screenToPlanPoint(
  pointer: { x: number; y: number },
  stage: Konva.Stage,
  t: Transform,
): [number, number] {
  const stageScale = stage.scaleX() || 1;
  const contentX = (pointer.x - stage.x()) / stageScale;
  const contentY = (pointer.y - stage.y()) / stageScale;
  return [
    (contentX - t.offsetX) / t.scaleX,
    (contentY - t.offsetY) / t.scaleY,
  ];
}

function projectPoint(
  x: number,
  y: number,
  projection: Projection,
): [number, number] {
  if (projection.viewMode === "iso") {
    const localX = x - projection.originX;
    const localY = y - projection.originY;
    const cosYaw = Math.cos(projection.orbit.yaw);
    const sinYaw = Math.sin(projection.orbit.yaw);
    const sinPitch = Math.sin(projection.orbit.pitch);
    const cosPitch = Math.cos(projection.orbit.pitch);
    const rotatedX = localX * cosYaw - localY * sinYaw;
    const rotatedY = localX * sinYaw + localY * cosYaw;

    return [
      rotatedX,
      rotatedY * sinPitch + projection.floorElevation * cosPitch,
    ];
  }
  return [x, y];
}

function projectedBounds(
  bounds: GeometryBounds,
  renderFloors: RenderFloorInstance[],
  floorBoundsByKey: Map<string, GeometryBounds>,
  viewMode: ViewMode,
  isoOrbit: IsoOrbit,
  alignmentOriginsByKey: Map<string, [number, number]>,
): GeometryBounds {
  if (viewMode === "plan") {
    return bounds;
  }

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const instance of renderFloors) {
    const floorBounds =
      floorBoundsByKey.get(floorSourceKey(instance.floor)) ?? bounds;
    const projection = projectionForFloor(
      viewMode,
      floorBounds,
      instance.floorElevation,
      isoOrbit,
      alignmentOriginsByKey.get(floorSourceKey(instance.floor)),
    );
    const corners = [
      [floorBounds.min_x, floorBounds.min_y],
      [floorBounds.min_x, floorBounds.max_y],
      [floorBounds.max_x, floorBounds.min_y],
      [floorBounds.max_x, floorBounds.max_y],
    ];

    for (const [x, y] of corners) {
      const [projectedX, projectedY] = projectPoint(x, y, projection);
      minX = Math.min(minX, projectedX);
      minY = Math.min(minY, projectedY);
      maxX = Math.max(maxX, projectedX);
      maxY = Math.max(maxY, projectedY);
    }
  }

  if (
    minX === Number.POSITIVE_INFINITY ||
    minY === Number.POSITIVE_INFINITY ||
    maxX === Number.NEGATIVE_INFINITY ||
    maxY === Number.NEGATIVE_INFINITY
  ) {
    return bounds;
  }

  return {
    min_x: minX,
    min_y: minY,
    max_x: maxX,
    max_y: maxY,
  };
}

function projectionForFloor(
  viewMode: ViewMode,
  floorBounds: GeometryBounds,
  floorElevation: number,
  isoOrbit: IsoOrbit,
  datumPoint?: [number, number],
): Projection {
  if (viewMode === "plan") {
    return {
      viewMode,
      floorElevation: 0,
      originX: 0,
      originY: 0,
      orbit: { yaw: 0, pitch: Math.PI / 2 },
    };
  }

  return {
    viewMode,
    floorElevation,
    originX: datumPoint?.[0] ?? (floorBounds.min_x + floorBounds.max_x) / 2,
    originY: datumPoint?.[1] ?? (floorBounds.min_y + floorBounds.max_y) / 2,
    orbit: isoOrbit,
  };
}

function buildAutoAlignmentOrigins(
  floors: FloorData[],
  floorBoundsByKey: Map<string, GeometryBounds>,
  fallbackBounds: GeometryBounds,
): Map<string, [number, number]> {
  const origins = new Map<string, [number, number]>();
  if (floors.length <= 1) return origins;

  const labelMaps = new Map<string, Map<string, [number, number]>>();
  for (const floor of floors) {
    labelMaps.set(floorSourceKey(floor), buildColumnLabelMap(floor));
  }

  const sortedFloors = [...floors].sort(
    (a, b) => firstFloorSortValue(a) - firstFloorSortValue(b),
  );
  const referenceFloor =
    sortedFloors.find(
      (floor) =>
        (labelMaps.get(floorSourceKey(floor))?.size ?? 0) >=
        MIN_ALIGNMENT_LABELS,
    ) ?? sortedFloors[0];
  const referenceKey = floorSourceKey(referenceFloor);
  const referenceBounds =
    floorBoundsByKey.get(referenceKey) ?? fallbackBounds;
  const referenceOrigin: [number, number] = [
    (referenceBounds.min_x + referenceBounds.max_x) / 2,
    (referenceBounds.min_y + referenceBounds.max_y) / 2,
  ];

  const offsets = new Map<string, [number, number]>([[referenceKey, [0, 0]]]);
  const pending = new Set(
    sortedFloors
      .map((floor) => floorSourceKey(floor))
      .filter((key) => key !== referenceKey),
  );

  while (pending.size > 0) {
    let progressed = false;

    for (const key of [...pending]) {
      const labels = labelMaps.get(key);
      if (!labels || labels.size < MIN_ALIGNMENT_LABELS) {
        pending.delete(key);
        continue;
      }

      let best:
        | {
            commonCount: number;
            residual: number;
            dx: number;
            dy: number;
          }
        | null = null;

      for (const [alignedKey, alignedOffset] of offsets) {
        const alignedLabels = labelMaps.get(alignedKey);
        if (!alignedLabels) continue;

        const delta = medianLabelDelta(alignedLabels, labels);
        if (!delta || delta.residual > MAX_ALIGNMENT_RESIDUAL_FT) continue;

        const candidate = {
          commonCount: delta.commonCount,
          residual: delta.residual,
          dx: alignedOffset[0] + delta.dx,
          dy: alignedOffset[1] + delta.dy,
        };

        if (
          !best ||
          candidate.commonCount > best.commonCount ||
          (candidate.commonCount === best.commonCount &&
            candidate.residual < best.residual)
        ) {
          best = candidate;
        }
      }

      if (!best) continue;

      offsets.set(key, [best.dx, best.dy]);
      pending.delete(key);
      progressed = true;
    }

    if (!progressed) break;
  }

  for (const [key, [dx, dy]] of offsets) {
    origins.set(key, [referenceOrigin[0] - dx, referenceOrigin[1] - dy]);
  }

  return origins;
}

function buildColumnLabelMap(floor: FloorData): Map<string, [number, number]> {
  const grouped = new Map<string, { x: number; y: number; count: number }>();

  for (const column of floor.columns) {
    const label = normalizeAlignmentLabel(column.label);
    if (!label) continue;

    const current = grouped.get(label);
    if (current) {
      current.x += column.point[0];
      current.y += column.point[1];
      current.count += 1;
    } else {
      grouped.set(label, {
        x: column.point[0],
        y: column.point[1],
        count: 1,
      });
    }
  }

  const labels = new Map<string, [number, number]>();
  for (const [label, value] of grouped) {
    labels.set(label, [value.x / value.count, value.y / value.count]);
  }
  return labels;
}

function normalizeAlignmentLabel(label: string): string | null {
  const normalized = label.trim().toUpperCase().replace(/\s+/g, "");
  if (!normalized || normalized.includes("UNLABELED")) return null;
  return normalized;
}

function medianLabelDelta(
  referenceLabels: Map<string, [number, number]>,
  targetLabels: Map<string, [number, number]>,
): { commonCount: number; dx: number; dy: number; residual: number } | null {
  const dxs: number[] = [];
  const dys: number[] = [];

  for (const [label, targetPoint] of targetLabels) {
    const referencePoint = referenceLabels.get(label);
    if (!referencePoint) continue;
    dxs.push(referencePoint[0] - targetPoint[0]);
    dys.push(referencePoint[1] - targetPoint[1]);
  }

  if (dxs.length < MIN_ALIGNMENT_LABELS) return null;

  const dx = median(dxs);
  const dy = median(dys);
  const residual = median(
    dxs.map((candidateDx, index) =>
      Math.hypot(candidateDx - dx, dys[index] - dy),
    ),
  );

  return { commonCount: dxs.length, dx, dy, residual };
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

function firstFloorSortValue(floor: FloorData): number {
  return floorStackSortValue(floor, expandFloorIdentifier(floor.floor_id)[0] ?? floor.floor_id);
}

function shouldStartPan(
  e: Konva.KonvaEventObject<MouseEvent>,
  viewMode: ViewMode,
  spaceHeld: boolean,
): boolean {
  const { button, shiftKey, altKey } = e.evt;
  // Space + any button, middle/right click, or shift/alt modifier always pan.
  if (spaceHeld) return true;
  if (button === 1 || button === 2 || shiftKey || altKey) return true;
  // Plan view: primary click on empty stage background pans.
  return viewMode === "plan" && button === 0 && e.target === e.target.getStage();
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function computeFloorBounds(floor: FloorData): GeometryBounds | null {
  const points: number[][] = [];

  if (floor.slab_boundary) {
    for (const ring of geometryToRings(floor.slab_boundary)) {
      points.push(...ring);
    }
  }

  for (const wall of floor.walls) {
    if (wall.wall_line) {
      points.push(...wall.wall_line.coordinates);
    }
  }

  for (const beam of floor.beams ?? []) {
    if (beam.beam_line) {
      points.push(...beam.beam_line.coordinates);
    }
  }

  for (const column of floor.columns) {
    points.push(column.point);
  }

  if (!points.length) return null;

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;

  for (const [x, y] of points) {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }

  return {
    min_x: minX,
    min_y: minY,
    max_x: maxX,
    max_y: maxY,
  };
}

function floorSourceKey(floor: FloorData): string {
  return `${floor.floor_index}:${floor.floor_id}`;
}

function normalizeAngle(angle: number): number {
  const fullTurn = Math.PI * 2;
  return ((angle % fullTurn) + fullTurn) % fullTurn;
}

function isUnlabeledColumn(label: string): boolean {
  return label.toUpperCase().includes("UNLABELED");
}

type VerticalConnection =
  | {
      kind: "column";
      lowerInstance: RenderFloorInstance;
      upperInstance: RenderFloorInstance;
      lowerPoint: [number, number];
      upperPoint: [number, number];
    }
  | {
      kind: "column-phantom";
      // Phantom column extension: a transfer column gets visually extended
      // one floor below its lowest existing floor so the red dot lands at
      // the transfer slab below.
      lowerInstance: RenderFloorInstance;
      upperInstance: RenderFloorInstance;
      lowerPoint: [number, number];
      upperPoint: [number, number];
    }
  | {
      kind: "wall";
      lowerInstance: RenderFloorInstance;
      upperInstance: RenderFloorInstance;
      // Matched vertex sequences (same length) — consecutive pairs form
      // a quad face of the wall when rendered in iso.
      lowerCoords: [number, number][];
      upperCoords: [number, number][];
      closed: boolean;
    };

function alignedPoint(
  point: [number, number],
  origin: [number, number] | undefined,
): [number, number] {
  if (!origin) return point;
  return [point[0] - origin[0], point[1] - origin[1]];
}

function translateBetweenOrigins(
  point: [number, number],
  fromOrigin: [number, number] | undefined,
  toOrigin: [number, number] | undefined,
): [number, number] {
  if (!fromOrigin || !toOrigin) return point;
  return [
    point[0] - fromOrigin[0] + toOrigin[0],
    point[1] - fromOrigin[1] + toOrigin[1],
  ];
}

function wallVertices(wall: { wall_line: { coordinates: number[][] } | null }): number[][] {
  const coords = wall.wall_line?.coordinates;
  if (!coords || coords.length < 2) return [];
  const first = coords[0];
  const last = coords[coords.length - 1];
  if (
    coords.length > 2 &&
    first[0] === last[0] &&
    first[1] === last[1]
  ) {
    return coords.slice(0, -1);
  }
  return coords;
}

function wallIsClosed(wall: { wall_line: { coordinates: number[][] } | null }): boolean {
  const coords = wall.wall_line?.coordinates;
  return Boolean(coords && coords.length >= 4);
}
