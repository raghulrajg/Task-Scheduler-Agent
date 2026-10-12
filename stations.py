"""
stations.py
-----------
The station / location metadata file (stations.json): where every place the
mobile robot drives to is, in the AMR's map frame.

    stations   every station drawn on the AMR's map (LM1, LM2, ...), copied
               from the robot's live map: x, y in metres, theta in radians.
    locations  the scheduler's location names (site.json: manufacturing_1,
               quality_station, ...) with the exact pose the robot must reach.
               A location is either linked to a station ("station": "LM1",
               coordinates copied from it) or taught/typed directly.

The ONIX driver reads this file at the start of EVERY navigation, so edits
take effect without restarting anything. Maintain it with
tools/amr_stations.py (pull / assign / teach / set / list / check).
"""

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_XY_TOLERANCE_M = 0.10
DEFAULT_YAW_TOLERANCE_DEG = 5.0


class LocationError(ValueError):
    """The location is unknown or has no usable coordinates."""


def empty() -> Dict[str, Any]:
    return {
        "_comment": "AMR station/location metadata. x, y in metres, theta in radians, AMR map frame. "
                    "Maintain with: python3 tools/amr_stations.py --help",
        "map": {},
        "defaults": {"xy_tolerance_m": DEFAULT_XY_TOLERANCE_M, "yaw_tolerance_deg": DEFAULT_YAW_TOLERANCE_DEG},
        "stations": {},
        "locations": {},
    }


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    base = empty()
    base.update(data)
    base.setdefault("stations", {})
    base.setdefault("locations", {})
    return base


def save(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def has_pose(entry: Optional[Dict[str, Any]]) -> bool:
    return bool(entry) and all(_num(entry.get(k)) for k in ("x", "y", "theta"))


def assigned_locations(data: Dict[str, Any]) -> List[str]:
    """Location names that have a complete x, y, theta."""
    return sorted(n for n, e in data.get("locations", {}).items() if has_pose(e))


def location_pose(data: Dict[str, Any], name: str) -> Tuple[float, float, float, float, float]:
    """(x, y, theta, xy_tolerance_m, yaw_tolerance_rad) for a location."""
    entry = data.get("locations", {}).get(name)
    if entry is None:
        raise LocationError(f"location {name!r} is not in the stations file")
    if not has_pose(entry):
        raise LocationError(f"location {name!r} has no x/y/theta yet -- assign it "
                            f"(tools/amr_stations.py assign {name} <station>) or teach it "
                            f"(tools/amr_stations.py teach {name})")
    d = data.get("defaults", {})
    xy_tol = float(entry.get("xy_tolerance_m", d.get("xy_tolerance_m", DEFAULT_XY_TOLERANCE_M)))
    yaw_tol = math.radians(float(entry.get("yaw_tolerance_deg", d.get("yaw_tolerance_deg",
                                                                      DEFAULT_YAW_TOLERANCE_DEG))))
    return float(entry["x"]), float(entry["y"]), float(entry["theta"]), xy_tol, yaw_tol


def wrap_angle(a: float) -> float:
    """Angle in (-pi, pi]."""
    return math.atan2(math.sin(a), math.cos(a))


def pose_error(x: float, y: float, theta: float, gx: float, gy: float, gtheta: float) -> Tuple[float, float]:
    """(distance in m, absolute heading error in rad)."""
    return math.hypot(x - gx, y - gy), abs(wrap_angle(theta - gtheta))


def locations_at(data: Dict[str, Any], x: float, y: float, theta: float) -> List[str]:
    """Every location whose pose the robot is within tolerance of, nearest first
    (two names can share one spot, e.g. rest_position on the same station)."""
    hits = []
    for name in assigned_locations(data):
        gx, gy, gt, xy_tol, yaw_tol = location_pose(data, name)
        d, yaw = pose_error(x, y, theta, gx, gy, gt)
        if d <= xy_tol and yaw <= yaw_tol:
            hits.append((d, name))
    return [name for _, name in sorted(hits)]


def location_at(data: Dict[str, Any], x: float, y: float, theta: float) -> Optional[str]:
    """The location the robot is currently at (within that location's tolerances), if any."""
    hits = locations_at(data, x, y, theta)
    return hits[0] if hits else None


def stations_from_map(map_body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Stations (advancedPointList) of an AMR map as {name: {class, x, y, theta}}."""
    out = {}
    for st in map_body.get("advancedPointList") or []:
        name = st.get("instanceName")
        pos = st.get("pos") or {}
        if not name or not _num(pos.get("x")) or not _num(pos.get("y")):
            continue
        out[name] = {"class": st.get("className", ""), "x": round(float(pos["x"]), 4),
                     "y": round(float(pos["y"]), 4), "theta": round(float(st.get("dir") or 0.0), 4)}
    return out


def map_info(map_body: Dict[str, Any], source: str) -> Dict[str, Any]:
    h = map_body.get("header", {})
    return {"name": (h.get("mapName") or "").strip(), "source": source,
            "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "frame": "AMR map frame: x, y in metres; theta in radians, counter-clockwise, 0 = +x axis"}


def refresh_from_map(data: Dict[str, Any], map_body: Dict[str, Any], source: str) -> List[str]:
    """Replace the station list from a map and update locations linked to a station.
    Returns human-readable notes about what changed."""
    notes = []
    new = stations_from_map(map_body)
    old = data.get("stations", {})
    for name in sorted(set(old) - set(new)):
        notes.append(f"station {name} is no longer on the map")
    for name in sorted(set(new) - set(old)):
        notes.append(f"new station {name} at ({new[name]['x']}, {new[name]['y']}, "
                     f"{math.degrees(new[name]['theta']):.1f} deg)")
    for name in sorted(set(new) & set(old)):
        d, yaw = pose_error(new[name]["x"], new[name]["y"], new[name]["theta"],
                            old[name]["x"], old[name]["y"], old[name]["theta"])
        if d > 0.005 or yaw > math.radians(0.5):
            notes.append(f"station {name} moved by {d:.3f} m / {math.degrees(yaw):.1f} deg")
    data["stations"] = new
    data["map"] = map_info(map_body, source)
    for loc, entry in data.get("locations", {}).items():
        st = entry.get("station")
        if not st:
            continue
        if st in new:
            if (entry.get("x"), entry.get("y"), entry.get("theta")) != (new[st]["x"], new[st]["y"], new[st]["theta"]):
                notes.append(f"location {loc} follows station {st}: coordinates updated")
            entry.update(x=new[st]["x"], y=new[st]["y"], theta=new[st]["theta"])
        else:
            notes.append(f"location {loc} is linked to {st}, which is not on the map any more -- "
                         f"its coordinates were cleared")
            entry.update(x=None, y=None, theta=None)
    return notes
