"""
tools/run_sim_fleet.py
----------------------
Start a simulated agent for every subsystem in robots_metadata.json, in one
process, so the whole pipeline (scheduler_service.py + reasoning agent +
HMI) can be exercised with no hardware.

    python3 tools/run_sim_fleet.py                       # 2 s navigate, 1 s pick/place/QC
    python3 tools/run_sim_fleet.py --speed 5 --qc-pass-rate 0.5 --fail-rate 0.05
"""

import argparse
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from drivers.sim import SimDriver  # noqa: E402
from robot_agent import RobotAgent  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--metadata", default=config.ROBOT_METADATA_PATH)
    p.add_argument("--speed", type=float, default=1.0, help="time scale divisor")
    p.add_argument("--qc-pass-rate", type=float, default=0.8)
    p.add_argument("--fail-rate", type=float, default=0.0)
    args = p.parse_args()

    with open(args.metadata) as f:
        robots = json.load(f)["robots"]

    agents = []
    for r in robots:
        topics = [s["topic"] for s in r["subsystems"].values()]
        for sub in r["subsystems"].values():
            actions = [c for c in sub["capabilities"] if c in ("navigate", "pick", "place", "quality_check")]
            driver = SimDriver({
                "actions": actions,
                "durations_s": {"navigate": 2 / args.speed, "pick": 1 / args.speed,
                                "place": 1 / args.speed, "quality_check": 1 / args.speed},
                "fail_rate": {a: args.fail_rate for a in actions},
                "qc_pass_rate": args.qc_pass_rate,
            })
            siblings = tuple(t for t in topics if t != sub["topic"])
            agent = RobotAgent(r["id"], sub["topic"], driver, heartbeat_s=3, interlock_with=siblings,
                               client_id=f"sim_{r['id']}_{sub['topic']}")
            agent.start()
            agents.append(agent)
            print(f"sim agent {r['id']}/{sub['topic']} actions={actions}")

    stop = threading.Event()
    try:
        while not stop.wait(3):
            for a in agents:
                a._publish_heartbeat()
    except KeyboardInterrupt:
        pass
    finally:
        for a in agents:
            a.shutdown()


if __name__ == "__main__":
    main()
