import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "web" / "api" / "_engine"
sys.path[:0] = [str(ENGINE), str(Path(__file__).parent), str(ROOT / "scripts")]

import aps_client  # noqa: E402
import revit_takedown_dxf  # noqa: E402
from fake_aps import FakeAps, zip_result  # noqa: E402
from fixture import EXPECTED_COLUMNS, EXPECTED_SLAB_SF, make_fixture  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def fake(monkeypatch):
    server = FakeAps()
    monkeypatch.setattr(aps_client, "BASE_URL", server.base)
    monkeypatch.setattr(aps_client, "DA_URL", f"{server.base}/da/us-east/v3")
    monkeypatch.setattr(aps_client, "OSS_URL", f"{server.base}/oss/v2")
    monkeypatch.setattr(aps_client, "MD_URL", f"{server.base}/modelderivative/v2")
    monkeypatch.setattr(aps_client, "_backoff", lambda attempt: 0)
    monkeypatch.setattr(aps_client.time, "sleep", lambda s: None)
    yield server
    server.close()


def _client():
    return aps_client.ApsClient("id", "secret", timeout=10)


def _blob(tmp_path, size):
    path = tmp_path / "model.rvt"
    path.write_bytes(os.urandom(size))
    return path


def test_multipart_upload_batches_retries_and_refreshes(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(aps_client, "MAX_URLS_PER_REQUEST", 2)
    path = _blob(tmp_path, 5 * MB * 4 + 123)
    fake.fail_once = {2: 403, 4: 503}
    result = _client().upload_file("b", "model.rvt", path, part_bytes=5 * MB, workers=3)
    assert result["size"] == path.stat().st_size
    assert fake.objects["b/model.rvt"] == path.read_bytes()
    assert sorted(set(fake.part_puts)) == [1, 2, 3, 4, 5]


def test_upload_resumes_only_missing_parts(fake, tmp_path):
    path = _blob(tmp_path, 5 * MB * 3 + 7)
    state = tmp_path / "state.json"
    fake.fail_always = {3}
    with pytest.raises(aps_client.ApsError):
        _client().upload_file("b", "model.rvt", path, part_bytes=5 * MB, workers=1, state_path=state)
    assert set(json.loads(state.read_text())["done"]) == {1, 2}
    fake.fail_always = set()
    fake.part_puts.clear()
    _client().upload_file("b", "model.rvt", path, part_bytes=5 * MB, workers=2, state_path=state)
    assert set(fake.part_puts) == {3, 4}
    assert fake.objects["b/model.rvt"] == path.read_bytes()
    assert not state.exists()


def test_upload_skips_existing_object_of_same_size(fake, tmp_path):
    path = _blob(tmp_path, 1024)
    fake.objects["b/model.rvt"] = path.read_bytes()
    _client().upload_file("b", "model.rvt", path)
    assert fake.part_puts == []


def test_part_size_grows_to_stay_under_part_limit(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(aps_client, "MAX_PARTS", 2)
    path = _blob(tmp_path, 5 * MB * 3)
    _client().upload_file("b", "model.rvt", path, part_bytes=5 * MB)
    assert set(fake.part_puts) == {1, 2}


def test_revit_json_to_dxf_one_level(tmp_path):
    report = revit_takedown_dxf.convert(make_fixture(), tmp_path / "plan.dxf", ["Level 2"])
    level = report["levels"][0]
    assert level["columns"] == EXPECTED_COLUMNS
    assert level["slab_area_sf"] == pytest.approx(EXPECTED_SLAB_SF, rel=1e-3)
    assert sorted(level["openings"]) == [64.0, 160.0]
    assert level["shear_walls_by_confidence"] == {"high": 2, "medium": 2, "low": 2}
    assert level["column_sizes"] == {"18x18": 13, "24x24": 6}
    assert all("Partition" not in w["type"] for w in level["walls"])


def test_ambiguous_core_is_reported(tmp_path):
    data = make_fixture()
    for wall in data["walls"]:
        wall.update(structuralFlag=False, structuralUsage="NonBearing", typeName='Generic - 8"',
                    materials=[{"name": "Default Wall", "materialClass": "Generic", "function": "Structure"}])
    level = revit_takedown_dxf.convert(data, tmp_path / "plan.dxf", ["Level 2"])["levels"][0]
    assert "high" not in level["shear_walls_by_confidence"]
    assert any("ambiguous" in w for w in level["warnings"])


def test_engine_consumes_revit_dxf(tmp_path):
    revit_takedown_dxf.convert(make_fixture(), tmp_path / "plan.dxf", ["Level 2"])
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_takedown_dxf.py"), str(tmp_path / "plan.dxf"),
         "--json", str(tmp_path / "v.json"), "--expect-columns", str(EXPECTED_COLUMNS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((tmp_path / "v.json").read_text())
    floor = report["summary"]["floors"][0]
    assert report["layer_mapping"]["opening"] == ["S-SLAB-OPNG"]
    assert report["layer_mapping"]["support_point"] == ["S-COLS"]
    assert floor["labelled_columns"] == EXPECTED_COLUMNS
    assert floor["slab_openings"] == 2
    assert floor["walls"] >= 4


def test_cli_run_end_to_end_against_fake_aps(fake, tmp_path, monkeypatch):
    import rvt2dxf

    fake.result_payload = zip_result(make_fixture())
    fake.activities["fakenick.TakedownExport+prod"] = {"id": "TakedownExport"}
    model = _blob(tmp_path, 6 * MB)
    monkeypatch.setenv("APS_CLIENT_ID", "id")
    monkeypatch.setenv("APS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(rvt2dxf, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(sys, "argv", ["rvt2dxf", "run", str(model), "--levels", "Level 2", "--out", str(tmp_path / "out"),
                                      "--part-mb", "5"])
    assert rvt2dxf.main() == 0
    out = tmp_path / "out"
    assert (out / "takedown_Level-2.dxf").exists()
    assert json.loads((out / "validation.json").read_text())["ok"] is True
    payload = fake.workitems["wi0"]["payload"]
    assert payload["arguments"]["rvtFile"]["url"].startswith("urn:adsk.objects:os.object:")
    assert payload["limitProcessingTimeSec"] == 3 * 3600


def test_heal_closes_small_gaps():
    from shapely.geometry import LineString
    from geometry_utils import heal_line_endpoints
    from shapely.ops import polygonize, unary_union

    gap = 0.02
    lines = [LineString([(0, 0), (10, 0)]), LineString([(10 + gap, 0), (10, 10)]),
             LineString([(10, 10 + gap), (0, 10)]), LineString([(0, 10), (0, gap)])]
    assert not list(polygonize(unary_union(lines)))
    polys = list(polygonize(unary_union(heal_line_endpoints(lines, 1 / 12))))
    assert len(polys) == 1 and polys[0].area == pytest.approx(100, rel=0.01)


def test_aia_layer_aliases():
    from inspection_utils import suggest_layers

    counts = {"S-COLS": {"LWPOLYLINE": 40}, "COL-IDEN": {"MTEXT": 40}, "S-SLAB": {"LWPOLYLINE": 2},
              "S-SLAB-OPNG": {"LWPOLYLINE": 3}, "S-SHEARWALL": {"LWPOLYLINE": 8}, "A-WALL-PRTN": {"LINE": 300}}
    suggestions = suggest_layers(counts)
    assert suggestions["support_point"] == ["S-COLS"]
    assert suggestions["column_label"] == ["COL-IDEN"]
    assert suggestions["boundary"] == ["S-SLAB"]
    assert suggestions["opening"] == ["S-SLAB-OPNG"]
    assert suggestions["wall"] == ["S-SHEARWALL"]
