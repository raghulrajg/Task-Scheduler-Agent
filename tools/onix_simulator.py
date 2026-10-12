"""
tools/onix_simulator.py
-----------------------
A stand-in for the ONIX robot that speaks the same API as the real one
(HTTP :3000 /map/live, WS :4000/nav, WS :7000/status). The simulated robot
drives to free-nav goals and reports its pose, speeds, battery, e-stop and
localization like the real robot does.

Use it to try the AMR agent and tools/amr_stations.py without the robot:

    python3 tools/onix_simulator.py                         # map + stations from stations.json
    python3 tools/onix_simulator.py --map amr_tools/maps/live_map.json --speed 2
    python3 tools/onix_simulator.py --ports 13000 14000 17000

then point an agent config at host 127.0.0.1 (and the same ports).
Also used by the automated tests (OnixSimulator class).
"""

import argparse
import asyncio
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WEB_APP_API_KEY = "043ea683c1d0b7b5a9f584ef2e6c36079c62a7e7f0a76606778bab99"


class OnixSimulator:
    def __init__(self, map_body: Optional[Dict[str, Any]] = None, ports=(0, 0, 0), speed: float = 0.5,
                 turn_speed: float = 1.0, status_hz: float = 10.0, start=(0.0, 0.0, 0.0),
                 api_key: str = WEB_APP_API_KEY, host: str = "127.0.0.1"):
        self.map = map_body or {"header": {"mapName": "sim"}, "advancedPointList": []}
        self.api_key, self.host = api_key, host
        self.x, self.y, self.theta = start
        self.speed, self.turn_speed, self.status_hz = speed, turn_speed, status_hz
        self.lin = self.ang = 0.0
        self.goal = None                 # (x, y, theta)
        self.battery = 87.0
        self.confidence = 95.0
        self.emergency = 0
        self.error_id = 0
        self.errors, self.warnings = [], []
        # fault injection
        self.reject_goals = False        # reply status false
        self.ignore_goals = False        # accept but never move
        self.block_after_m = None        # stop (blocked) after driving this far
        self.stop_offset_m = 0.0         # stop this far short of the goal (positioning error)
        self.final_yaw_error = 0.0       # rad left at the end
        self.requests = []               # (api_id, request) received
        self._driven = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        rest, control, status = ports
        self._http = ThreadingHTTPServer((host, rest), self._http_handler())
        threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._ws_ports = {}
        threading.Thread(target=self._run_ws, args=(control, status), daemon=True).start()
        self._ready.wait(5)
        threading.Thread(target=self._physics, daemon=True).start()

    # ---- ports ----------------------------------------------------------------------------
    @property
    def ports(self) -> Dict[str, int]:
        return {"rest": self._http.server_address[1], "control": self._ws_ports["control"],
                "status": self._ws_ports["status"]}

    def close(self):
        self._stop.set()
        self._http.shutdown()
        self._http.server_close()

        async def shutdown():
            for server in self._servers:
                server.close()
            for server in self._servers:
                await server.wait_closed()
            self._done.set_result(None)
        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(5)
        except Exception:
            pass

    # ---- physics ----------------------------------------------------------------------------
    def _physics(self):
        dt = 0.02
        while not self._stop.wait(dt):
            with self._lock:
                self.lin = self.ang = 0.0
                if self.goal is None or self.emergency or self.ignore_goals:
                    continue
                gx, gy, gt = self.goal
                d = math.hypot(gx - self.x, gy - self.y)
                stop_at = self.stop_offset_m
                if self.block_after_m is not None and self._driven >= self.block_after_m:
                    continue                                        # blocked: stands still
                if d > stop_at + 1e-3:
                    step = min(self.speed * dt, d - stop_at)
                    heading = math.atan2(gy - self.y, gx - self.x)
                    self.x += step * math.cos(heading)
                    self.y += step * math.sin(heading)
                    self._driven += step
                    self.lin = self.speed
                    continue
                target_t = gt + self.final_yaw_error
                err = math.atan2(math.sin(target_t - self.theta), math.cos(target_t - self.theta))
                if abs(err) > 1e-3:
                    step = max(-self.turn_speed * dt, min(self.turn_speed * dt, err))
                    self.theta += step
                    self.ang = self.turn_speed if step > 0 else -self.turn_speed
                    continue
                self.goal = None                                    # arrived (robot's own view)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"percentage": self.battery, "x": self.x, "y": self.y, "angle": self.theta,
                    "confidence": self.confidence, "emergency": self.emergency, "error_id": self.error_id,
                    "linear_speed": round(self.lin, 3), "angular_speed": round(self.ang, 3),
                    "power_supply_status": 2, "signal_strength": -50}

    def set_goal(self, x, y, theta):
        with self._lock:
            self.goal = (float(x), float(y), float(theta))
            self._driven = 0.0

    # ---- servers -------------------------------------------------------------------------------
    def _http_handler(self):
        sim = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/map/live"):
                    body = {"status": True, "data": sim.map}
                elif self.path.startswith("/params"):
                    body = {"robot_dimensions": {"width": 0.85, "height": 0.75}, "lidar_offsets": {}}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        return H

    def _run_ws(self, control_port, status_port):
        from websockets.asyncio.server import serve
        asyncio.set_event_loop(self._loop)

        async def main():
            control = await serve(self._control_handler, self.host, control_port)
            status = await serve(self._status_handler, self.host, status_port)
            self._servers = [control, status]
            self._ws_ports["control"] = control.sockets[0].getsockname()[1]
            self._ws_ports["status"] = status.sockets[0].getsockname()[1]
            self._done = asyncio.get_running_loop().create_future()
            self._ready.set()
            await self._done

        self._loop.run_until_complete(main())
        self._loop.close()

    def _decode(self, raw) -> Optional[Dict[str, Any]]:
        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        if msg.get("key") != self.api_key:
            return {"_bad_key": True}
        return msg.get("request") or {}

    async def _control_handler(self, ws):
        async for raw in ws:
            req = self._decode(raw)
            if req is None or req.get("_bad_key"):
                await ws.send(json.dumps({"status": False, "message": "invalid key", "error_code": 401}))
                continue
            api = req.get("api_id")
            self.requests.append((api, req))
            if api == "4002":
                if self.reject_goals:
                    await ws.send(json.dumps({"status": False, "message": "goal rejected", "error_code": 3}))
                    continue
                g = req.get("free_nav", {})
                self.set_goal(g.get("linear_x"), g.get("linear_y"), g.get("angular_z"))
                await ws.send(json.dumps({"status": True, "message": "navigation started", "error_code": 0}))
            elif api == "4003":
                name = req.get("path_nav", {}).get("target_node")
                st = next((p for p in self.map.get("advancedPointList") or [] if p.get("instanceName") == name), None)
                if st is None:
                    await ws.send(json.dumps({"status": False, "message": f"no station {name}", "error_code": 4}))
                    continue
                self.set_goal(st["pos"]["x"], st["pos"]["y"], st.get("dir", 0))
                await ws.send(json.dumps({"status": True, "message": "navigation started", "error_code": 0}))
            elif api == "4004":
                with self._lock:
                    self.goal = None
                await ws.send(json.dumps({"status": True, "message": "cancelled", "error_code": 0}))
            else:
                await ws.send(json.dumps({"status": False, "message": f"unknown api {api}", "error_code": 1}))

    async def _status_handler(self, ws):
        try:
            req = self._decode(await ws.recv())
        except Exception:
            return
        if req is None or req.get("_bad_key"):
            return
        api = req.get("api_id")
        period = 1.0 / self.status_hz
        try:
            while not self._stop.is_set() and not self._done.done():
                if api == "7004":
                    data = self.status()
                elif api == "7002":
                    if self.confidence < 70:
                        await asyncio.sleep(period)
                        continue
                    s = self.status()
                    data = {"x": s["x"], "y": s["y"], "angle": s["angle"]}
                elif api == "7010":
                    data = {"errors": list(self.errors), "warnings": list(self.warnings)}
                else:
                    await asyncio.sleep(period)
                    continue
                await ws.send(json.dumps({"status": True, "api_id": api, "data": data}))
                await asyncio.sleep(period)
        except Exception:
            return


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--map", help="AMR map JSON (exported live map); default: stations from stations.json")
    p.add_argument("--stations", default="stations.json")
    p.add_argument("--ports", nargs=3, type=int, default=[3000, 4000, 7000], metavar=("REST", "CONTROL", "STATUS"))
    p.add_argument("--speed", type=float, default=0.5, help="m/s")
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    if args.map:
        with open(args.map) as f:
            map_body = json.load(f)
    else:
        import stations as st
        data = st.load(args.stations)
        map_body = {"header": {"mapName": "simulated"}, "advancedPointList": [
            {"instanceName": n, "className": s.get("class", "LocationMark"),
             "pos": {"x": s["x"], "y": s["y"]}, "dir": s["theta"]} for n, s in data["stations"].items()]}
    sim = OnixSimulator(map_body, ports=tuple(args.ports), speed=args.speed, status_hz=1.0, host=args.host)
    print(f"ONIX simulator on {args.host}: {sim.ports}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(2)
            s = sim.status()
            print(f"pose ({s['x']:.2f}, {s['y']:.2f}, {math.degrees(s['angle']):.0f} deg) "
                  f"speed {s['linear_speed']} goal {sim.goal}")
    except KeyboardInterrupt:
        sim.close()


if __name__ == "__main__":
    main()
