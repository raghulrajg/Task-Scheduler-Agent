"""stations.json logic and tools/amr_stations.py (against the ONIX simulator for live commands)."""
import json
import math
import os
import subprocess
import sys

import pytest

import stations as st
from tools.onix_simulator import OnixSimulator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = {"header": {"mapName": "Plant A "}, "advancedPointList": [
    {"instanceName": "LM1", "className": "LocationMark", "pos": {"x": -1.15, "y": 0.05}, "dir": 0.0},
    {"instanceName": "LM2", "className": "LocationMark", "pos": {"x": -3.82, "y": 0.01}, "dir": math.pi / 2},
]}


def test_stations_from_map_and_refresh_follows_links():
    data = st.empty()
    st.refresh_from_map(data, MAP, "test")
    assert data["stations"]["LM2"] == {"class": "LocationMark", "x": -3.82, "y": 0.01, "theta": round(math.pi / 2, 4)}
    data["locations"]["manufacturing_1"] = {"station": "LM1", **{k: data["stations"]["LM1"][k] for k in "x y theta".split()}}
    moved = json.loads(json.dumps(MAP))
    moved["advancedPointList"][0]["pos"]["x"] = -1.00
    notes = st.refresh_from_map(data, moved, "test")
    assert any("LM1 moved by 0.150 m" in n for n in notes)
    assert data["locations"]["manufacturing_1"]["x"] == -1.0
    gone = {"header": {}, "advancedPointList": [MAP["advancedPointList"][1]]}
    notes = st.refresh_from_map(data, gone, "test")
    assert not st.has_pose(data["locations"]["manufacturing_1"]) and any("cleared" in n for n in notes)


def test_location_pose_tolerances_and_location_at():
    data = st.empty()
    data["defaults"] = {"xy_tolerance_m": 0.1, "yaw_tolerance_deg": 5}
    data["locations"]["a"] = {"x": 0.0, "y": 0.0, "theta": 0.0}
    data["locations"]["b"] = {"x": 2.0, "y": 0.0, "theta": math.pi, "xy_tolerance_m": 0.02, "yaw_tolerance_deg": 1}
    data["locations"]["c"] = {"station": None, "x": None, "y": None, "theta": None}
    assert st.assigned_locations(data) == ["a", "b"]
    assert st.location_pose(data, "b")[3:] == (0.02, pytest.approx(math.radians(1)))
    with pytest.raises(st.LocationError, match="assign"):
        st.location_pose(data, "c")
    assert st.location_at(data, 0.05, 0.0, math.radians(3)) == "a"
    assert st.location_at(data, 0.05, 0.0, math.radians(8)) is None          # heading off
    assert st.location_at(data, 2.0, 0.0, -math.pi + 0.001) == "b"           # angle wrap
    assert st.location_at(data, 2.03, 0.0, math.pi) is None


def run_tool(args, cwd_files):
    return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "amr_stations.py")] + args,
                          capture_output=True, text=True, timeout=60, cwd=ROOT)


def test_tool_pull_assign_set_teach_where_check(tmp_path):
    sim = OnixSimulator(map_body=MAP, start=(0.4, -0.2, math.radians(30)), status_hz=10)
    try:
        f = str(tmp_path / "stations.json")
        agent = tmp_path / "amr.json"
        agent.write_text(json.dumps({"driver": {"type": "onix", "host": "127.0.0.1", "ports": sim.ports,
                                                "stations_file": f}}))
        base = ["--agent", str(agent)]
        r = run_tool(base + ["pull"], None)
        assert r.returncode == 0, r.stderr
        assert "2 station(s) from 'Plant A'" in r.stdout
        assert run_tool(base + ["assign", "manufacturing_1", "LM1", "--xy-tol", "0.05"], None).returncode == 0
        assert run_tool(base + ["set", "waste_area", "1", "2", "90"], None).returncode == 0
        r = run_tool(base + ["teach", "package_area"], None)
        assert r.returncode == 0 and "taught" in r.stdout, r.stderr
        data = st.load(f)
        assert data["locations"]["manufacturing_1"] == {"station": "LM1", "x": -1.15, "y": 0.05, "theta": 0.0,
                                                        "xy_tolerance_m": 0.05}
        assert data["locations"]["waste_area"]["theta"] == pytest.approx(math.pi / 2)
        assert data["locations"]["package_area"]["x"] == pytest.approx(0.4, abs=1e-3)
        r = run_tool(base + ["where"], None)
        assert "at location: package_area" in r.stdout
        r = run_tool(base + ["check"], None)
        assert r.returncode == 1 and "[MISS] quality_station" in r.stdout
        assert run_tool(base + ["assign", "quality_station", "LM2"], None).returncode == 0
        assert run_tool(base + ["assign", "rest_position", "LM1"], None).returncode == 0
        assert run_tool(base + ["check"], None).returncode == 0
        assert "No station 'LM9'" in run_tool(base + ["assign", "x", "LM9"], None).stderr
        sim.confidence = 20
        assert "not localized" in run_tool(base + ["teach", "rest_position"], None).stderr
    finally:
        sim.close()


def test_repository_stations_file_matches_the_robot_map():
    data = st.load(os.path.join(ROOT, "stations.json"))
    with open(os.path.join(ROOT, "amr_tools", "maps", "live_map.json")) as fh:
        live = json.load(fh)
    assert data["stations"] == st.stations_from_map(live)
