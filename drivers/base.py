"""
drivers/base.py
---------------
The contract between robot_agent.py and a piece of hardware.

A driver turns one subtask (navigate / pick / place / quality_check) into
real vendor API calls and blocks until the action is finished, failed or
cancelled. It never talks MQTT -- the agent does that.

Rules every driver must follow:
  * execute() must watch the `cancel` event and stop the hardware promptly
    when it is set, returning cancelled().
  * execute() must never return done() unless the vendor confirmed success.
    When in doubt, return failed() -- the scheduler escalates to a human.
  * get_state() must be cheap and must not raise for normal "robot not
    ready" situations; it reports them in the returned dict.
"""

import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


def done(result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"state": "done"}
    if result is not None:
        out["result"] = result
    return out


def failed(error: str, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"state": "failed", "error": error}
    if result is not None:
        out["result"] = result
    return out


def cancelled(reason: str = "cancelled") -> Dict[str, Any]:
    return {"state": "cancelled", "error": reason}


def robot_state(ready: bool, detail: str = "", estop: bool = False,
                battery: Optional[float] = None) -> Dict[str, Any]:
    return {"ready": bool(ready), "detail": detail, "estop": bool(estop), "battery": battery}


class Driver(ABC):
    #: actions this driver can execute, e.g. ("navigate",)
    actions: Tuple[str, ...] = ()

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    @abstractmethod
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        """Run one subtask to completion. Returns done()/failed()/cancelled()."""

    def cancel(self) -> None:
        """Called from another thread right after `cancel` is set. Use it to
        send an immediate stop to the hardware if execute() is blocked."""

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """Return robot_state(...) for the heartbeat."""

    def close(self) -> None:
        pass
