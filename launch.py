"""
launch.py
---------
One command per machine role. Starts the right programs, prefixes their
output, restarts any that crash, and stops them all cleanly on Ctrl+C.
Broker settings (your EMQX) come from .env.

  Preflight (no robot moves):
    python3 launch.py check                      # EMQX login + pub/sub, Ollama, running scheduler, agents online
    python3 launch.py check robot_01             # ... plus each robot_01 agent's hardware state

  TEST mode (simulated robots on your EMQX, isolated under the "sim/" topics):
    python3 launch.py test                       # scheduler + simulated fleet
    python3 launch.py test --with-llm            # ... + reasoning agent (Ollama)
    python3 launch.py test --jobs 5              # ... and submit 5 jobs once everything is up
    python3 launch.py test --hardware agents/robot_01_arm.json   # real Dobot joins, rest simulated

  REAL mode (the real robots):
    python3 launch.py server                     # central server: scheduler + reasoning agent
    python3 launch.py server --no-llm            # scheduler only
    python3 launch.py robot robot_01             # robot PC: every agents/robot_01_*.json
    python3 launch.py agents agents/qc_01_scanner.json   # explicit agent configs

Talk to a running stack from another terminal (add --test for test mode):
    python3 manual_runner.py [--test]            # submit one job and follow it
    python3 tools/fleetctl.py [--test] status    # robots + jobs
"""

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

PY = sys.executable
COLORS = ["\033[36m", "\033[35m", "\033[33m", "\033[32m", "\033[34m", "\033[91m"]
RESET = "\033[0m" if sys.stdout.isatty() else ""
if not sys.stdout.isatty():
    COLORS = [""] * len(COLORS)
_print_lock = threading.Lock()


def out(line: str):
    with _print_lock:
        print(line, flush=True)


# =============================================================================== supervisor
class Child:
    def __init__(self, name: str, argv: List[str], env: Dict[str, str], color: str, restart: bool = True):
        self.name, self.argv, self.env, self.color, self.restart = name, argv, env, color, restart
        self.proc: Optional[subprocess.Popen] = None
        self.backoff = 1.0
        self.started_at = 0.0

    def start(self):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0  # same console: Ctrl+C reaches the children too
        self.proc = subprocess.Popen(self.argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, **kwargs)
        self.started_at = time.time()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        tag = f"{self.color}[{self.name}]{RESET}"
        for line in proc.stdout:
            out(f"{tag} {line.rstrip()}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()  # SIGTERM on Linux: agents cancel the running action first


def supervise(children: List[Child], after_start=None):
    stopping = threading.Event()

    def on_signal(*_):
        stopping.set()
    signal.signal(signal.SIGTERM, on_signal)

    for c in children:
        c.start()
        out(f"started {c.name}: {' '.join(os.path.relpath(a, HERE) if a.startswith(HERE) else a for a in c.argv[1:])}")
    if after_start:
        threading.Thread(target=after_start, daemon=True).start()
    try:
        while not stopping.wait(1):
            for c in children:
                code = c.proc.poll()
                if code is None:
                    if time.time() - c.started_at > 60:
                        c.backoff = 1.0
                    continue
                if not c.restart:
                    continue
                out(f"!! {c.name} exited with code {code}; restarting in {c.backoff:.0f}s")
                time.sleep(c.backoff)
                c.backoff = min(c.backoff * 2, 30)
                c.start()
    except KeyboardInterrupt:
        pass
    out("stopping...")
    for c in children:
        c.stop()
    deadline = time.time() + 25
    for c in children:
        try:
            c.proc.wait(max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            out(f"!! {c.name} did not stop in time; killing it")
            c.proc.kill()
    out("all stopped")


def env_for(mode: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["RUN_MODE"] = mode
    env["PYTHONUNBUFFERED"] = "1"
    return env


# =============================================================================== roles
def role_test(args):
    env = env_for("test")
    skip, hardware = [], []
    for cfg_path in args.hardware:
        with open(cfg_path) as f:
            cfg = json.load(f)
        skip += ["--skip", f"{cfg['robot_id']}/{cfg['subsystem']}"]
        hardware.append(cfg_path)
    children = [
        Child("scheduler", [PY, "scheduler_service.py"], env, COLORS[0]),
        Child("sim-fleet", [PY, "tools/run_sim_fleet.py", "--speed", str(args.speed),
                            "--qc-pass-rate", str(args.qc_pass_rate), "--fail-rate", str(args.fail_rate)] + skip,
              env, COLORS[1]),
    ]
    for i, cfg_path in enumerate(hardware):
        name = os.path.splitext(os.path.basename(cfg_path))[0]
        children.append(Child(f"HW {name}", [PY, "robot_agent.py", "--config", cfg_path], env, COLORS[2 + i % 3]))
    if args.with_llm:
        children.append(Child("reasoning", [PY, "reasoning_agent.py"], env, COLORS[5]))

    def submit_jobs():
        time.sleep(6)
        for i in range(args.jobs):
            subprocess.run([PY, "tools/fleetctl.py", "submit"], env=env, stdout=subprocess.DEVNULL)
        if args.jobs:
            out(f">> submitted {args.jobs} job(s). Watch the [scheduler] lines, or: python3 tools/fleetctl.py --test status")

    out("TEST mode: simulated robots on your broker, isolated under the 'sim/' topics. Ctrl+C to stop.")
    out("Other terminal:  python3 manual_runner.py --test     |  python3 tools/fleetctl.py --test status")
    supervise(children, after_start=submit_jobs if args.jobs else None)


def role_server(args):
    env = env_for("real")
    children = [Child("scheduler", [PY, "scheduler_service.py"], env, COLORS[0])]
    if not args.no_llm:
        children.append(Child("reasoning", [PY, "reasoning_agent.py"], env, COLORS[5]))
    out("REAL mode: central server. Ctrl+C to stop.")
    supervise(children)


def agent_children(config_paths: List[str], env) -> List[Child]:
    children = []
    for i, path in enumerate(config_paths):
        name = os.path.splitext(os.path.basename(path))[0]
        children.append(Child(name, [PY, "robot_agent.py", "--config", path], env, COLORS[(i + 2) % len(COLORS)]))
    return children


def role_robot(args):
    paths = []
    for rid in args.robot_ids:
        found = sorted(glob.glob(os.path.join("agents", f"{rid}_*.json")))
        if not found:
            sys.exit(f"no agent configs found for {rid} (expected agents/{rid}_<subsystem>.json)")
        paths += found
    out("REAL mode: robot agents. Ctrl+C stops them (each cancels its running action first).")
    supervise(agent_children(paths, env_for("real")))


def role_agents(args):
    for p in args.configs:
        if not os.path.exists(p):
            sys.exit(f"{p} not found")
    out("REAL mode: robot agents. Ctrl+C to stop.")
    supervise(agent_children(args.configs, env_for("real")))


# =============================================================================== preflight
def role_check(args):
    os.environ["RUN_MODE"] = "test" if args.test else "real"
    import config
    from mqtt_common import make_client, parse_json, parse_robot_topic, state_topic
    ok = True

    def line(status: str, text: str):
        out(f"  [{status:4}] {text}")

    out(f"\n{config.describe_mode()}")
    out(f"\nBroker  {config.MQTT_BROKER_HOST}:{config.MQTT_BROKER_PORT}  user={config.MQTT_USERNAME or '-'}  "
        f"tls={config.MQTT_TLS}")
    result = {}
    got = threading.Event()
    probe = config.topic(f"fleet/preflight/{uuid.uuid4().hex[:8]}")
    status_topic = config.topic(config.FLEET_STATUS_TOPIC)
    heartbeats = {}
    c = make_client(f"preflight_{uuid.uuid4().hex[:6]}", clean_session=True)

    def on_connect(cl, u, flags, rc, props):
        result["rc"] = str(rc)
        if not rc.is_failure:
            cl.subscribe([(probe, 1), (status_topic, 0), (state_topic("+", "+"), 1)])

    def on_subscribe(cl, u, mid, rcs, props):
        cl.publish(probe, b"ping", qos=1)

    def on_message(cl, u, msg):
        if msg.topic == probe:
            got.set()
        elif msg.topic == status_topic:
            result["status"] = parse_json(msg)
        else:
            parsed = parse_robot_topic(msg.topic)
            payload = parse_json(msg)
            if parsed and isinstance(payload, dict):
                heartbeats[f"{parsed[0]}/{parsed[1]}"] = payload

    c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
    try:
        c.connect(config.MQTT_BROKER_HOST, config.MQTT_BROKER_PORT, keepalive=10)
        c.loop_start()
        end = time.time() + 8
        while "rc" not in result and time.time() < end:
            time.sleep(0.1)
        if "rc" not in result:
            ok = False
            line("FAIL", "no answer from the broker (wrong host/port, firewall, or TLS mismatch?)")
        elif result["rc"] != "Success":
            ok = False
            line("FAIL", f"broker refused the login: {result['rc']} (check MQTT_USERNAME / MQTT_PASSWORD)")
        else:
            line("OK", "logged in")
            if got.wait(5):
                line("OK", f"publish + subscribe work (topic namespace: '{config.TOPIC_PREFIX or '(root)'}')")
            else:
                ok = False
                line("FAIL", "connected but could not publish/subscribe (EMQX authorization rules?)")
            time.sleep(1.5)  # collect retained status + heartbeats
            snap = result.get("status")
            if isinstance(snap, dict):
                age = time.time() - float(snap.get("ts", 0))
                line("OK" if age < 30 else "WARN", f"scheduler status seen ({age:.0f}s old): "
                     f"{len(snap.get('tasks', []))} task(s), {snap.get('pending', 0)} waiting")
            else:
                line("INFO", "no scheduler running in this mode yet")
            if heartbeats:
                for name, hb in sorted(heartbeats.items()):
                    state = "online" if hb.get("online") else "OFFLINE"
                    extra = f" ready={hb.get('ready')} battery={hb.get('battery')} {hb.get('detail', '')}"
                    line("OK" if hb.get("online") else "WARN", f"agent {name}: {state}{extra if hb.get('online') else ''}")
            else:
                line("INFO", "no robot agents have connected in this mode yet")
        c.loop_stop()
        c.disconnect()
    except OSError as e:
        ok = False
        line("FAIL", f"cannot reach the broker: {e}")

    out(f"\nOllama  {config.OLLAMA_HOST}:{config.OLLAMA_PORT}  model={config.OLLAMA_MODEL}")
    try:
        with urllib.request.urlopen(f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/tags", timeout=5) as r:
            models = [m.get("name") for m in json.loads(r.read()).get("models", [])]
        if config.OLLAMA_MODEL in models or any(m.split(":")[0] == config.OLLAMA_MODEL for m in models):
            line("OK", f"model available ({len(models)} installed)")
        else:
            line("WARN", f"'{config.OLLAMA_MODEL}' not installed; have {models}  (ollama pull {config.OLLAMA_MODEL})")
    except Exception as e:
        line("WARN", f"Ollama not reachable: {e}  (only needed for natural-language prompts)")

    out("\nConfig")
    try:
        from robot_registry import RobotRegistry
        from validation import SiteConfig
        site = SiteConfig.load(config.SITE_CONFIG_PATH)
        reg = RobotRegistry(config.ROBOT_METADATA_PATH)
        line("OK", f"site.json: {len(site.locations)} locations; robots_metadata.json: {len(reg.all())} robots")
    except Exception as e:
        ok = False
        line("FAIL", f"config files: {e}")

    for rid in args.robot_ids:
        out(f"\nHardware  {rid}  (state only; nothing moves)")
        paths = sorted(glob.glob(os.path.join("agents", f"{rid}_*.json")))
        if not paths:
            ok = False
            line("FAIL", f"no agents/{rid}_*.json")
        for p in paths:
            try:
                from drivers import create_driver
                with open(p) as f:
                    cfg = json.load(f)
                drv = create_driver(cfg["driver"])
                st = drv.get_state()
                if hasattr(drv, "check_result_channel"):
                    st["result_channel"] = drv.check_result_channel()
                good = st.get("ready") and st.get("result_channel", "ok") in ("ok", "disabled")
                line("OK" if good else "WARN", f"{os.path.basename(p)}: {json.dumps(st)}")
                drv.close()
            except Exception as e:
                ok = False
                line("FAIL", f"{os.path.basename(p)}: {type(e).__name__}: {e}")

    out("\n" + ("Preflight passed." if ok else "Preflight found problems (see FAIL lines)."))
    sys.exit(0 if ok else 1)


# =============================================================================== CLI
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="role", required=True)

    c = sub.add_parser("check", help="preflight: broker, Ollama, config, optional robot hardware state")
    c.add_argument("robot_ids", nargs="*")
    c.add_argument("--test", action="store_true", help="check the test-mode namespace instead")
    c.set_defaults(fn=role_check)

    t = sub.add_parser("test", help="TEST mode: scheduler + simulated fleet on your broker")
    t.add_argument("--with-llm", action="store_true", help="also run the reasoning agent (Ollama)")
    t.add_argument("--jobs", type=int, default=0, help="submit this many jobs after start-up")
    t.add_argument("--speed", type=float, default=1.0, help="simulated robots run this many times faster")
    t.add_argument("--qc-pass-rate", type=float, default=0.8)
    t.add_argument("--fail-rate", type=float, default=0.0, help="probability a simulated action fails")
    t.add_argument("--hardware", action="append", default=[], metavar="AGENT_CONFIG",
                   help="run this REAL agent in the test (its subsystem is not simulated)")
    t.set_defaults(fn=role_test)

    s = sub.add_parser("server", help="REAL mode: scheduler (+ reasoning agent)")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=role_server)

    r = sub.add_parser("robot", help="REAL mode: all agents of these robots (agents/<id>_*.json)")
    r.add_argument("robot_ids", nargs="+")
    r.set_defaults(fn=role_robot)

    a = sub.add_parser("agents", help="REAL mode: these agent config files")
    a.add_argument("configs", nargs="+")
    a.set_defaults(fn=role_agents)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
