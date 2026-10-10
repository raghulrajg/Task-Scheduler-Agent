"""
A fake UR controller: Dashboard Server (line protocol, responses per the
PolyScope 5 Dashboard Server manual) + Modbus TCP server (FC3/FC6). Used to
test drivers/ur.py without a robot. It is NOT a substitute for testing
against URSim or the real arm.
"""
import socket
import socketserver
import struct
import threading
import time


class FakeUR:
    def __init__(self, run_time=0.3, outcome=1, programs=("pick_mfg.urp", "place_pkg.urp")):
        self.robotmode = "RUNNING"
        self.safety = "NORMAL"
        self.remote = True
        self.program_state = "STOPPED"
        self.loaded = None
        self.programs = set(programs)
        self.run_time = run_time
        self.outcome = outcome           # value the "program" writes to register 128
        self.safety_on_finish = "NORMAL"
        self.registers = {128: 0}
        self.commands = []
        self._run_started = None
        self._lock = threading.Lock()
        self.dash = self._serve(self._dash_handler())
        self.modbus = self._serve(self._modbus_handler())

    @property
    def dash_port(self):
        return self.dash.server_address[1]

    @property
    def modbus_port(self):
        return self.modbus.server_address[1]

    def close(self):
        for srv in (self.dash, self.modbus):
            srv.shutdown()
            srv.server_close()

    def _serve(self, handler):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return srv

    def _tick(self):
        with self._lock:
            if (self.program_state == "PLAYING" and self._run_started is not None
                    and time.time() - self._run_started >= self.run_time):
                self.program_state = "STOPPED"
                self.registers[128] = self.outcome
                self.safety = self.safety_on_finish

    def respond(self, cmd: str) -> str:
        self.commands.append(cmd)
        self._tick()
        if cmd == "robotmode":
            return f"Robotmode: {self.robotmode}"
        if cmd == "safetystatus":
            return f"Safetystatus: {self.safety}"
        if cmd == "is in remote control":
            return "true" if self.remote else "false"
        if cmd == "programState":
            return f"{self.program_state} {self.loaded or ''}".strip()
        if cmd.startswith("load "):
            name = cmd[5:]
            if name not in self.programs:
                return f"File not found: {name}"
            self.loaded = name
            return f"Loading program: {name}"
        if cmd == "play":
            if not self.remote or not self.loaded:
                return "Failed to execute: play"
            with self._lock:
                self.program_state = "PLAYING"
                self._run_started = time.time()
            return "Starting program"
        if cmd == "stop":
            with self._lock:
                self.program_state = "STOPPED"
            return "Stopped"
        return f"could not understand: '{cmd}'"

    def _dash_handler(self):
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"Connected: Universal Robots Dashboard Server\n")
                for raw in self.rfile:
                    reply = fake.respond(raw.decode().strip())
                    self.wfile.write((reply + "\n").encode())
        return Handler

    def _modbus_handler(self):
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock: socket.socket = self.request
                while True:
                    head = sock.recv(7)
                    if len(head) < 7:
                        return
                    tid, proto, length, unit = struct.unpack(">HHHB", head)
                    pdu = sock.recv(length - 1)
                    fc = pdu[0]
                    if fc == 3:
                        addr, qty = struct.unpack(">HH", pdu[1:5])
                        fake._tick()
                        vals = b"".join(struct.pack(">H", fake.registers.get(addr + i, 0)) for i in range(qty))
                        resp = bytes([3, len(vals)]) + vals
                    elif fc == 6:
                        addr, val = struct.unpack(">HH", pdu[1:5])
                        fake.registers[addr] = val
                        resp = pdu[:5]
                    else:
                        resp = bytes([fc | 0x80, 1])
                    sock.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
        return Handler
