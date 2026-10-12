"""
tools/amr_stations.py
---------------------
Maintain stations.json: the x, y, theta of every station on the AMR's map,
and of every scheduler location (manufacturing_1, quality_station, ...).

    python3 tools/amr_stations.py pull                      # stations from the robot's live map
    python3 tools/amr_stations.py pull --map amr_tools/maps/live_map.json   # ... or from an exported map
    python3 tools/amr_stations.py list                      # stations, locations, what is missing
    python3 tools/amr_stations.py assign manufacturing_1 LM1   # location = that station's pose
    python3 tools/amr_stations.py teach package_area        # location = where the robot stands NOW
    python3 tools/amr_stations.py set waste_area -2.5 1.2 90   # x y theta(deg), typed in
    python3 tools/amr_stations.py set waste_area -2.5 1.2 90 --xy-tol 0.05 --yaw-tol 3
    python3 tools/amr_stations.py unassign waste_area
    python3 tools/amr_stations.py where                     # robot pose and the location it is at
    python3 tools/amr_stations.py check                     # every site.json location has a pose?

Robot address, ports and the stations file are taken from the AMR agent
config (default agents/robot_01_amr.json); override with --host / --file.
Nothing here moves the robot.
"""

import argparse
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import stations as st  # noqa: E402


def agent_settings(path):
    try:
        with open(path) as f:
            cfg = json.load(f)["driver"]
    except (OSError, KeyError, ValueError):
        return {}
    return cfg if cfg.get("type") == "onix" else {}


def api(args):
    from drivers.onix import WEB_APP_API_KEY, OnixAPI
    ports = args.agent_cfg.get("ports", {})
    host = args.host or args.agent_cfg.get("host")
    if not host:
        sys.exit("No robot address: use --host <ip> or set it in the AMR agent config")
    return OnixAPI(host, args.agent_cfg.get("api_key") or WEB_APP_API_KEY, int(ports.get("rest", 3000)),
                   int(ports.get("control", 4000)), int(ports.get("status", 7000)))


def load_or_new(path):
    return st.load(path) if os.path.exists(path) else st.empty()


def site_locations():
    try:
        with open(os.path.join(ROOT, "site.json")) as f:
            return list(json.load(f).get("locations", {}))
    except (OSError, ValueError):
        return []


def fmt_pose(e):
    if not st.has_pose(e):
        return "-- not set --"
    return f"x={e['x']:8.3f}  y={e['y']:8.3f}  theta={math.degrees(e['theta']):7.1f} deg"


def apply_tolerances(entry, args):
    if getattr(args, "xy_tol", None) is not None:
        entry["xy_tolerance_m"] = args.xy_tol
    if getattr(args, "yaw_tol", None) is not None:
        entry["yaw_tolerance_deg"] = args.yaw_tol


# ---- commands ------------------------------------------------------------------------------
def cmd_pull(args):
    data = load_or_new(args.file)
    if args.map:
        with open(args.map) as f:
            body = json.load(f)
        body = body.get("data", body) if "advancedPointList" not in body else body
        source = os.path.relpath(args.map, ROOT)
    else:
        a = api(args)
        print(f"Reading the live map from {a.host} ...")
        body = a.live_map()
        source = f"live map of AMR {a.host}"
    notes = st.refresh_from_map(data, body, source)
    for name in site_locations():
        data["locations"].setdefault(name, {"station": None, "x": None, "y": None, "theta": None})
    st.save(args.file, data)
    print(f"{len(data['stations'])} station(s) from '{data['map'].get('name')}' saved to {args.file}")
    for n in notes:
        print("  " + n)
    cmd_list(args)


def cmd_list(args):
    data = load_or_new(args.file)
    m = data.get("map", {})
    print(f"\n{args.file}   map: {m.get('name', '?')}   ({m.get('source', 'no map pulled yet')}, {m.get('pulled_at', '')})")
    print("\nSTATIONS (from the AMR map)")
    for name, s in sorted(data["stations"].items()):
        print(f"  {name:12} {fmt_pose(s)}   {s.get('class', '')}")
    if not data["stations"]:
        print("  none -- run: python3 tools/amr_stations.py pull")
    print("\nLOCATIONS (what the scheduler uses)")
    d = data.get("defaults", {})
    for name, e in sorted(data["locations"].items()):
        link = f"= {e['station']}" if e.get("station") else ("taught" if e.get("taught_at") else "")
        tol = f"tol {e.get('xy_tolerance_m', d.get('xy_tolerance_m'))} m / " \
              f"{e.get('yaw_tolerance_deg', d.get('yaw_tolerance_deg'))} deg"
        print(f"  {name:16} {fmt_pose(e)}   {link:10} {tol}")
    missing = [n for n in site_locations() if n not in st.assigned_locations(data)]
    if missing:
        print(f"\n  NOT SET: {', '.join(missing)}  -> assign / teach / set them before using them in jobs")


def cmd_assign(args):
    data = load_or_new(args.file)
    s = data["stations"].get(args.station)
    if s is None:
        sys.exit(f"No station {args.station!r}. Known: {', '.join(sorted(data['stations'])) or 'none (run pull)'}")
    entry = data["locations"].setdefault(args.location, {})
    entry.update(station=args.station, x=s["x"], y=s["y"], theta=s["theta"])
    entry.pop("taught_at", None)
    apply_tolerances(entry, args)
    st.save(args.file, data)
    print(f"{args.location} = station {args.station}: {fmt_pose(entry)}")


def cmd_teach(args):
    a = api(args)
    s = a.read("7004", timeout=5)
    conf = float(s.get("confidence") or 0)
    if conf < 70:
        sys.exit(f"The robot is not localized ({conf:.0f}%). Relocate it in the web app first.")
    if abs(float(s.get("linear_speed") or 0)) > 0.02 or abs(float(s.get("angular_speed") or 0)) > 0.02:
        sys.exit("The robot is moving. Teach only when it stands still at the exact spot.")
    data = load_or_new(args.file)
    entry = data["locations"].setdefault(args.location, {})
    entry.update(station=None, x=round(float(s["x"]), 4), y=round(float(s["y"]), 4),
                 theta=round(float(s["angle"]), 4), taught_at=__import__("time").strftime("%Y-%m-%dT%H:%M:%S"))
    apply_tolerances(entry, args)
    st.save(args.file, data)
    print(f"{args.location} taught from the robot's pose: {fmt_pose(entry)}")


def cmd_set(args):
    data = load_or_new(args.file)
    entry = data["locations"].setdefault(args.location, {})
    entry.update(station=None, x=args.x, y=args.y, theta=round(math.radians(args.theta_deg), 6))
    entry.pop("taught_at", None)
    apply_tolerances(entry, args)
    st.save(args.file, data)
    print(f"{args.location}: {fmt_pose(entry)}")


def cmd_unassign(args):
    data = load_or_new(args.file)
    if args.location not in data["locations"]:
        sys.exit(f"No location {args.location!r}")
    data["locations"][args.location] = {"station": None, "x": None, "y": None, "theta": None}
    st.save(args.file, data)
    print(f"{args.location} cleared")


def cmd_where(args):
    s = api(args).read("7004", timeout=5)
    data = load_or_new(args.file)
    x, y, th = float(s["x"]), float(s["y"]), float(s["angle"])
    print(f"pose x={x:.3f} y={y:.3f} theta={math.degrees(th):.1f} deg   localization {float(s.get('confidence') or 0):.0f}%")
    at = st.location_at(data, x, y, th)
    print(f"at location: {at or '-- none (outside every tolerance) --'}")
    for name in st.assigned_locations(data):
        gx, gy, gt, _, _ = st.location_pose(data, name)
        d, yaw = st.pose_error(x, y, th, gx, gy, gt)
        print(f"  {name:16} {d:6.3f} m  {math.degrees(yaw):6.1f} deg away")


def cmd_check(args):
    data = load_or_new(args.file)
    ok = True
    for name in site_locations():
        if name in st.assigned_locations(data):
            print(f"  [OK  ] {name}: {fmt_pose(data['locations'][name])}")
        else:
            ok = False
            print(f"  [MISS] {name}: no pose -- jobs using it will be rejected")
    for name, e in data["locations"].items():
        if e.get("station") and e["station"] not in data["stations"]:
            ok = False
            print(f"  [FAIL] {name} is linked to {e['station']}, which is not on the map")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="stations file (default: from the AMR agent config, else stations.json)")
    p.add_argument("--agent", default="agents/robot_01_amr.json", help="AMR agent config to read host/ports from")
    p.add_argument("--host", help="robot IP (overrides the agent config)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("pull")
    pl.add_argument("--map", help="exported map JSON instead of the live robot")
    pl.set_defaults(fn=cmd_pull)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = sub.add_parser("assign")
    a.add_argument("location")
    a.add_argument("station")
    t = sub.add_parser("teach")
    t.add_argument("location")
    s = sub.add_parser("set")
    s.add_argument("location")
    s.add_argument("x", type=float)
    s.add_argument("y", type=float)
    s.add_argument("theta_deg", type=float)
    for sp, fn in ((a, cmd_assign), (t, cmd_teach), (s, cmd_set)):
        sp.add_argument("--xy-tol", type=float, help="arrival tolerance in metres for this location")
        sp.add_argument("--yaw-tol", type=float, help="arrival tolerance in degrees for this location")
        sp.set_defaults(fn=fn)
    u = sub.add_parser("unassign")
    u.add_argument("location")
    u.set_defaults(fn=cmd_unassign)
    sub.add_parser("where").set_defaults(fn=cmd_where)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    args = p.parse_args()
    args.agent_cfg = agent_settings(args.agent)
    args.file = args.file or args.agent_cfg.get("stations_file") or "stations.json"
    args.fn(args)


if __name__ == "__main__":
    main()
