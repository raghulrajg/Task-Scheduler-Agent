"""
Fake Dobot controller (dashboard port) following the reply format of the
Dobot TCP/IP Remote Control Interface Guide: "ErrorID,{values},Cmd(args);".
For testing drivers/dobot.py only -- not a substitute for the real arm.
"""
import re
import socketserver
import threading
import time


class FakeDobot:
    def __init__(self, protocol="v4", run_time=0.3, outcome=1, projects=("pick_mfg", "place_pkg")):
        self.protocol = protocol
        self.mode = 5
        self.alarms = "[]"
        self.run_time = run_time
        self.outcome = outcome            # value the project writes to output register 0
        self.mode_on_finish = 5
        self.projects = set(projects)
        self.registers = {0: 0}
        self.commands = []
        self.control_requested = False
        self.joints = [0.0, -30.0, 120.0, 0.0, 90.0, 0.0]
        self._started = None
        self._lock = threading.Lock()
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    line = raw.decode().strip()
                    if line:
                        self.wfile.write((fake.respond(line) + "\n").encode())

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _tick(self):
        with self._lock:
            if self.mode == 7 and self._started is not None and time.time() - self._started >= self.run_time:
                self.mode = self.mode_on_finish
                self.registers[0] = self.outcome
                self._started = None

    def respond(self, cmd: str) -> str:
        self.commands.append(cmd)
        self._tick()
        name, args = re.match(r"^(\w+)\((.*)\)$", cmd).groups()
        lname = name.lower()

        def ok(values=""):
            return f"0,{{{values}}},{cmd};"

        def err(code):
            return f"{code},{{}},{cmd};"

        if lname == "robotmode":
            return ok(self.mode)
        if lname == "getangle":
            return ok(",".join(f"{j:.4f}" for j in self.joints))
        if lname == "geterrorid":
            return ok(self.alarms)
        if lname == "requestcontrol":
            if self.protocol == "v3":
                return err(-10000)
            self.control_requested = True
            return ok()
        if lname == "enablerobot":
            if self.mode in (3, 4):
                self.mode = 5
            return ok()
        if lname == "runscript":
            project = args.strip('"')
            if self.protocol == "v4" and not args.startswith('"'):
                return err(-1)
            if project not in self.projects or self.mode != 5:
                return err(-1)
            with self._lock:
                self.mode = 7
                self._started = time.time()
            return ok()
        if lname == "stop" and self.protocol == "v4" or lname == "stopscript" and self.protocol == "v3":
            with self._lock:
                self.mode = 5
                self._started = None
            return ok()
        if lname in ("setoutputint", "getoutputint"):
            if self.protocol == "v3":
                return err(-10000)
            parts = [p.strip() for p in args.split(",")]
            if lname == "setoutputint":
                self.registers[int(parts[0])] = int(parts[1])
                return ok()
            return ok(self.registers.get(int(parts[0]), 0))
        return err(-10000)
