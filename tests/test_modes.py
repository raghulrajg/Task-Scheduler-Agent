"""Real/test mode isolation on a shared broker."""
import os
import subprocess
import sys

import config
from mqtt_common import parse_robot_topic, state_topic, task_topic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_topics_are_namespaced_and_foreign_namespace_ignored(monkeypatch):
    monkeypatch.setattr(config, "TOPIC_PREFIX", "sim/")
    assert task_topic("robot_01", "amr") == "sim/robots/robot_01/amr/task"
    assert parse_robot_topic("sim/robots/robot_01/arm/state") == ("robot_01", "arm", "state")
    assert parse_robot_topic("robots/robot_01/arm/state") is None          # real traffic invisible to test
    monkeypatch.setattr(config, "TOPIC_PREFIX", "")
    assert state_topic("qc_01", "scanner") == "robots/qc_01/scanner/state"
    assert parse_robot_topic("sim/robots/robot_01/arm/state") is None      # test traffic invisible to real


def run(args, mode=None):
    env = {k: v for k, v in os.environ.items() if k not in ("RUN_MODE", "MQTT_TOPIC_PREFIX")}
    if mode:
        env["RUN_MODE"] = mode
    return subprocess.run([sys.executable] + args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)


def test_config_test_mode_defaults():
    r = run(["-c", "import config; print(config.RUN_MODE, repr(config.TOPIC_PREFIX), config.CLIENT_ID_SUFFIX, "
                   "config.SCHEDULER_STATE_PATH)"], mode="test")
    assert r.stdout.split() == ["test", "'sim/'", "-test", "state/test_scheduler_state.json"]
    r = run(["-c", "import config; print(config.RUN_MODE, repr(config.TOPIC_PREFIX), "
                   "repr(config.CLIENT_ID_SUFFIX), config.SCHEDULER_STATE_PATH)"])
    assert r.stdout.split() == ["real", "''", "''", "state/scheduler_state.json"]


def test_cli_flag_selects_test_mode():
    r = run(["-c", "import sys; sys.argv=['x','--test']; import modes; modes.apply_cli_mode(); "
                   "import config; print(config.RUN_MODE, sys.argv)"])
    assert r.stdout.strip() == "test ['x']"


def test_sim_fleet_refuses_real_mode():
    r = run(["tools/run_sim_fleet.py"])
    assert r.returncode != 0 and "only run in TEST mode" in (r.stderr + r.stdout)


def test_sim_agent_refused_in_real_mode_unless_stand_in(tmp_path):
    cfg = tmp_path / "a.json"
    cfg.write_text('{"robot_id": "qc_01", "subsystem": "scanner", "driver": {"type": "sim"}}')
    r = run(["robot_agent.py", "--config", str(cfg)])
    assert r.returncode != 0 and "simulated driver in REAL mode" in r.stderr
