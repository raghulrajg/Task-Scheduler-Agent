"""
tools/run_sim_fleet.py
----------------------
Start a simulated agent for every subsystem in robots_metadata.json, in one
process, so the whole pipeline (scheduler_service.py + reasoning agent +
HMI) can be exercised with no hardware.

    python3 tools/run_sim_fleet.py --test                       # 2 s navigate, 1 s pick/place/QC
    python3 tools/run_sim_fleet.py --test --speed 5 --qc-pass-rate 0.5 --fail-rate 0.05

Refuses to run in real mode: simulated robots must never share topics with real ones.

Like the real fleet, each simulated base reports where it is (at_location)
and which locations it has coordinates for (every site.json location, or
--locations), and each simulated arm refuses to pick/place unless its base
reports it is at the task's location ("require_base_at_zone").
"""

import argparse
import json
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modes  # noqa: E402
modes.apply_cli_mode()
import config  # noqa: E402
from drivers.sim import SimDriver  # noqa: E402
from robot_agent import RobotAgent  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metadata", default=config.ROBOT_METADATA_PATH)
    p.add_argument("--speed", type=float, default=1.0, help="time scale divisor")
    p.add_argument("--qc-pass-rate", type=float, default=0.8)
    p.add_argument("--fail-rate", type=float, default=0.0)
    p.add_argument("--skip", action="append", default=[], metavar="ROBOT/SUBSYSTEM",
                   help="don't simulate this one, e.g. robot_01/arm when the real Dobot joins the test")
    p.add_argument("--site", default=config.SITE_CONFIG_PATH)
    p.add_argument("--locations", help="comma-separated locations the simulated bases have coordinates "
                                       "for (default: all site.json locations)")
    args = p.parse_args()
    if not config.TEST_MODE:
        sys.exit("Simulated robots only run in TEST mode, so they can never answer on the real robots' "
                 "topics.\nUse:  python3 tools/run_sim_fleet.py --test   (or: python3 launch.py test)")
    print(config.describe_mode())

    with open(args.metadata) as f:
        robots = json.load(f)["robots"]
    if args.locations:
        locations = [x.strip() for x in args.locations.split(",") if x.strip()]
    else:
        with open(args.site) as f:
            locations = sorted(json.load(f)["locations"])

    agents = []
    for r in robots:
        topics = [s["topic"] for s in r["subsystems"].values()]
        for sub in r["subsystems"].values():
            if f"{r['id']}/{sub['topic']}" in args.skip:
                print(f"skipping {r['id']}/{sub['topic']} (real hardware joins the test)")
                continue
            actions = [c for c in sub["capabilities"] if c in ("navigate", "pick", "place", "quality_check")]
            driver = SimDriver({
                "actions": actions,
                "durations_s": {"navigate": 2 / args.speed, "pick": 1 / args.speed,
                                "place": 1 / args.speed, "quality_check": 1 / args.speed},
                "fail_rate": {a: args.fail_rate for a in actions},
                "qc_pass_rate": args.qc_pass_rate,
                "report_location": True, "locations": locations,
            })
            siblings = tuple(t for t in topics if t != sub["topic"])
            base = next((s["topic"] for s in r["subsystems"].values()
                         if "navigate" in s["capabilities"] and s["topic"] != sub["topic"]), None)
            arm = any(c in ("pick", "place") for c in actions)
            agent = RobotAgent(r["id"], sub["topic"], driver, heartbeat_s=3, interlock_with=siblings,
                               require_base_at_zone=base if arm else None,
                               client_id=f"sim_{r['id']}_{sub['topic']}")
            agent.start()
            agents.append(agent)
            print(f"sim agent {r['id']}/{sub['topic']} actions={actions}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(3):
            for a in agents:
                a._publish_heartbeat()
    except KeyboardInterrupt:
        pass
    finally:
        threads = [threading.Thread(target=a.shutdown) for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)


if __name__ == "__main__":
    main()
