"""
Hardware drivers for robot_agent.py. Pick one per subsystem in the agent's
config file with "type". Add a new vendor by subclassing drivers.base.Driver
and registering it in DRIVERS below.
"""

import os
import re
from typing import Any, Dict

from drivers.base import Driver

_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve_env(value: Any) -> Any:
    """Replace "${VAR}" strings with environment values, recursively, so
    secrets stay in .env / systemd EnvironmentFile instead of JSON configs."""
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    if isinstance(value, str):
        m = _ENV_RE.match(value)
        if m:
            if m.group(1) not in os.environ:
                raise KeyError(f"config references ${{{m.group(1)}}} but it is not set in the environment")
            return os.environ[m.group(1)]
    return value


def create_driver(cfg: Dict[str, Any]) -> Driver:
    cfg = _resolve_env(cfg)
    kind = cfg.get("type")
    if kind == "sim":
        from drivers.sim import SimDriver
        return SimDriver(cfg)
    if kind == "mir":
        from drivers.mir import MiRDriver
        return MiRDriver(cfg)
    if kind == "ur":
        from drivers.ur import URDriver
        return URDriver(cfg)
    if kind == "dobot":
        from drivers.dobot import DobotDriver
        return DobotDriver(cfg)
    if kind == "http_amr":
        from drivers.http_amr import HttpAmrDriver
        return HttpAmrDriver(cfg)
    if kind == "http_inspection":
        from drivers.inspection import HttpInspectionDriver
        return HttpInspectionDriver(cfg)
    if kind == "tcp_inspection":
        from drivers.inspection import TcpInspectionDriver
        return TcpInspectionDriver(cfg)
    raise ValueError(f"unknown driver type {kind!r}; expected sim|http_amr|dobot|mir|ur|http_inspection|tcp_inspection")
