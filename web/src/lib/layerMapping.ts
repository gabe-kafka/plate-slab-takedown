import type { DraftData, LayerMapping } from "./types";

export const EMPTY_LAYER_MAPPING: LayerMapping = {
  boundary: [],
  additional_load: [],
  opening: [],
  wall: [],
  beam: [],
  support_point: [],
  column_label: [],
  floor_label: [],
  datum: [],
};

export function layerMappingFromDraft(draft: DraftData): LayerMapping {
  return {
    boundary: draft.suggestions.boundary || [],
    additional_load: draft.suggestions.additional_load || [],
    opening: draft.suggestions.opening || [],
    wall: draft.suggestions.wall || [],
    beam: draft.suggestions.beam || [],
    support_point: draft.suggestions.support_point || [],
    column_label: draft.suggestions.column_label || [],
    floor_label: draft.suggestions.floor_label || [],
    datum: draft.suggestions.datum || [],
  };
}
