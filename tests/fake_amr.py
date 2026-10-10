"""
A fake web-dashboard AMR (token login + JSON status/goto/cancel), shaped like
what such robots typically expose. Used to test drivers/http_amr.py; the
real robot's endpoints come from the capture (docs/AMR_API_CAPTURE.md).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAMR:
    def __init__(self, points=("station_A", "station_B"), drive_time=0.6, pickup_delay=0.3):
        self.points = set(points)
        self.state, self.target = "SUCCEEDED", "station_B"   # left over from a previous goal
        self.drive_time, self.pickup_delay = drive_time, pickup_delay
        self.final_state = "SUCCEEDED"
        self.never_start = False
        self.emergency = False
        self.error_msg = ""
        self.battery = 0.87
        self.token = "tok-1"
        self.requests = []
        self._goal = None
        self._lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n)) if n else None

            def _authed(self):
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, {"code": 401, "msg": "unauthorized"})
                    return False
                return True

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, body))
                if self.path == "/api/login":
                    if body == {"username": "admin", "password": "pw{1}"}:
                        return self._send(200, {"code": 0, "data": {"token": fake.token}})
                    return self._send(403, {"code": 403})
                if not self._authed():
                    return
                if self.path == "/api/nav/goto":
                    if body.get("point") not in fake.points:
                        return self._send(200, {"code": 1, "msg": "no such point"})
                    fake.goto(body["point"])
                    return self._send(200, {"code": 0})
                if self.path == "/api/nav/cancel":
                    with fake._lock:
                        fake._goal = None
                        fake.state = "CANCELED"
                    return self._send(200, {"code": 0})
                self._send(404, {})

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if not self._authed():
                    return
                if self.path == "/api/robot/status":
                    fake._tick()
                    return self._send(200, {"code": 0, "data": {
                        "nav": {"state": fake.state, "target": fake.target},
                        "battery": fake.battery, "emergency": fake.emergency, "error_msg": fake.error_msg}})
                self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()

    def goto(self, point):
        with self._lock:
            if point == self.target and self.state == "SUCCEEDED":
                return  # already there: many robots just keep reporting success
            self._goal = (point, time.time())

    def _tick(self):
        with self._lock:
            if not self._goal or self.never_start or self.emergency:
                return
            point, t0 = self._goal
            t = time.time() - t0
            if t >= self.pickup_delay:
                self.state, self.target = "RUNNING", point
            if t >= self.pickup_delay + self.drive_time:
                self.state = self.final_state
                if self.final_state == "FAILED":
                    self.error_msg = "path blocked"
                self._goal = None

    def nav_posts(self):
        return [r for r in self.requests if r[1] == "/api/nav/goto"]
