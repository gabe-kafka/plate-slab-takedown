import type { DemoId } from "./api";

export interface DemoProject {
  /** URL slug: /demo/<slug> opens the project in ISO view with tributaries on. */
  slug: string;
  id: DemoId;
  name: string;
  label: string;
  filename: string;
}

export const DEMO_PROJECTS: DemoProject[] = [
  { slug: "358-flatbush", id: "default", name: "358 Flatbush", label: "Load 358 Demo 1 (good example)", filename: "358 Flatbush - input.dxf" },
  { slug: "1025-atlantic", id: "geom_clean_1", name: "1025 Atlantic", label: "Load 1025 Demo 2 (good example)", filename: "1025 Atlantic - input.dxf" },
  { slug: "356-fulton", id: "fulton_356", name: "356 Fulton", label: "Load 356 Demo 3 (best reference)", filename: "356 Fulton - input.dxf" },
  { slug: "246-franklin", id: "franklin_246", name: "246 Franklin", label: "Load 246 Demo 4", filename: "246 Franklin - input.dxf" },
  { slug: "1300-manhattan", id: "manhattan_1300", name: "1300 Manhattan", label: "Load 1300 Demo 5 (hillside, arch background)", filename: "1300 Manhattan - input.dxf" },
];

export function findDemo(slug: string): DemoProject | undefined {
  const key = slug.toLowerCase();
  return DEMO_PROJECTS.find((d) => d.slug === key || d.id === key);
}
