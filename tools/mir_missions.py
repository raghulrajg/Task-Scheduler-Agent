"""
tools/mir_missions.py
---------------------
List the missions on a MiR robot with their GUIDs, to fill in the
"locations" section of an agents/*_amr.json config.

    python3 tools/mir_missions.py --config agents/robot_01_amr.json
    python3 tools/mir_missions.py --host 192.168.12.20 --username distributor --password ...
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from drivers import _resolve_env  # noqa: E402
from drivers.mir import MiRDriver  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", help="agent config with a mir driver section")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--username")
    p.add_argument("--password")
    args = p.parse_args()

    if args.config:
        with open(args.config) as f:
            cfg = _resolve_env(json.load(f)["driver"])
    else:
        cfg = {"type": "mir", "host": args.host, "port": args.port,
               "username": args.username, "password": args.password}
    drv = MiRDriver(cfg)

    status = drv._status()
    print(f"Robot: {status.get('robot_name')} ({status.get('robot_model')}) "
          f"state={status.get('state_text')} battery={status.get('battery_percentage')}%\n")
    code, missions = drv._request("GET", "/missions")
    if code != 200:
        sys.exit(f"GET /missions -> HTTP {code}: {missions}")
    print(f"{'GUID':38}  NAME")
    for m in missions:
        print(f"{m.get('guid', ''):38}  {m.get('name', '')}")
    mapped = cfg.get("locations", {})
    if mapped:
        known = {m.get("guid") for m in missions}
        print("\nConfigured locations:")
        for zone, loc in mapped.items():
            ok = "OK " if loc.get("mission_guid") in known else "MISSING"
            print(f"  [{ok}] {zone:20} -> {loc.get('mission_guid')}")


if __name__ == "__main__":
    main()
