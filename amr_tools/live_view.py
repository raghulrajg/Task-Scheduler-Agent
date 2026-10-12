"""
live_view.py - live picture of the ONIX robot.

Shows the live map, stations, paths, areas and reflectors, plus the robot's
position, lidar points and status, updated 5 times a second.

Install:
    pip install requests websocket-client matplotlib

Run (onix_client.py must be in the same folder):
    python live_view.py                            live window
    python live_view.py --host 1.2.3.4             different robot address
    python live_view.py --save snapshot.png        save one picture, no window
"""

from __future__ import annotations

import argparse
import math
import threading
import time

from onix_client import DEFAULT_HOST, ChannelSilentError, OnixClient

# Colours
COLOR_MAP = "0.35"
COLOR_STATION = "#0066C5"
COLOR_PATH = "#dd6023"
COLOR_ROBOT = "green"
COLOR_LIDAR = "red"
COLOR_REFLECTOR = "purple"

REFRESH_MS = 200     # window update interval
STALE_AFTER_S = 2.0  # ignore pose / lidar older than this


# =============================================================================
# Path shapes (match how the web app draws each path type)
# =============================================================================

def bezier_points(p0: dict, p1: dict, p2: dict, p3: dict, n: int = 40) -> list:
    """Cubic Bezier curve (BezierPath)."""
    out = []
    for i in range(n + 1):
        t = i / n
        w = ((1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3)
        out.append((sum(wi * p["x"] for wi, p in zip(w, (p0, p1, p2, p3))),
                    sum(wi * p["y"] for wi, p in zip(w, (p0, p1, p2, p3)))))
    return out


def arc_points(p0: dict, p1: dict, p2: dict, n: int = 40) -> list:
    """Circular arc from p0 through p1 to p2 (ArcPath)."""
    ax, ay, bx, by, cx, cy = p0["x"], p0["y"], p1["x"], p1["y"], p2["x"], p2["y"]
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # points in a line
        return [(ax, ay), (cx, cy)]
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a_start, a_mid, a_end = (math.atan2(y - uy, x - ux) for x, y in ((ax, ay), (bx, by), (cx, cy)))
    sweep = (a_end - a_start) % (2 * math.pi)
    if (a_mid - a_start) % (2 * math.pi) > sweep:  # go the other way round
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a_start + sweep * i / n), uy + r * math.sin(a_start + sweep * i / n))
            for i in range(n + 1)]


def bspline_points(ctrl: list, degree: int, knots: list, n: int = 60) -> list:
    """B-spline curve (NURBS6; the app uses weights of 1)."""
    def basis(i: int, k: int, t: float) -> float:
        if k == 0:
            inside = knots[i] <= t < knots[i + 1]
            at_end = t == knots[-1] and knots[i] < t <= knots[i + 1]
            return 1.0 if inside or at_end else 0.0
        left = 0.0 if knots[i + k] == knots[i] else \
            (t - knots[i]) / (knots[i + k] - knots[i]) * basis(i, k - 1, t)
        right = 0.0 if knots[i + k + 1] == knots[i + 1] else \
            (knots[i + k + 1] - t) / (knots[i + k + 1] - knots[i + 1]) * basis(i + 1, k - 1, t)
        return left + right

    out = []
    for s in range(n + 1):
        w = [basis(i, degree, s / n) for i in range(len(ctrl))]
        out.append((sum(wi * p["x"] for wi, p in zip(w, ctrl)),
                    sum(wi * p["y"] for wi, p in zip(w, ctrl))))
    return out


def path_points(path: dict, params: dict) -> list:
    """Points along any path from the map's advancedCurveList."""
    start, end = path["startPos"]["pos"], path["endPos"]["pos"]
    kind = path.get("className")
    try:
        if kind == "BezierPath":
            return bezier_points(start, path["controlPos1"], path["controlPos2"], end)
        if kind == "ArcPath":
            return arc_points(start, path["controlPos1"], end)
        if kind == "NURBS6":
            ctrl = [start] + [path[f"controlPos{i}"] for i in range(1, 5)] + [end]
            return bspline_points(ctrl, params.get("degree", 3),
                                  params.get("knots", [0, 0, 0, 0, .4, .6, 1, 1, 1, 1]))
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return [(start["x"], start["y"]), (end["x"], end["y"])]  # StraightLine or unknown


# =============================================================================
# Background data (keeps the window responsive)
# =============================================================================

class LiveData:
    """Latest status, pose and lidar scan, kept up to date by background threads."""

    def __init__(self, client: OnixClient):
        self.client = client
        self._lock = threading.Lock()
        self._status = None
        self._pose, self._pose_time = None, 0.0
        self._scan, self._scan_time = None, 0.0
        self._error = ""

    def start(self) -> None:
        for channel, store in (("robot_status", self._store_status),
                               ("robot_pose", self._store_pose),
                               ("lidar", self._store_scan)):
            threading.Thread(target=self._follow, args=(channel, store), daemon=True).start()

    def _follow(self, channel: str, store) -> None:
        while True:
            try:
                for data in self.client.stream(channel, timeout=5):
                    with self._lock:
                        store(data)
            except ChannelSilentError:
                continue                    # nothing sent; ask again
            except Exception as e:          # robot offline, network problem...
                with self._lock:
                    self._error = f"{channel}: {e}"
                time.sleep(2)

    def _store_status(self, data) -> None:
        self._status = data

    def _store_pose(self, data) -> None:
        self._pose, self._pose_time = data, time.time()

    def _store_scan(self, data) -> None:
        self._scan, self._scan_time = data, time.time()

    def snapshot(self):
        """(status, pose, pose_is_live, scan, error) using only fresh data."""
        with self._lock:
            now = time.time()
            pose_is_live = self._pose is not None and now - self._pose_time < STALE_AFTER_S
            if pose_is_live:
                pose = self._pose
            elif self._status:
                pose = {k: self._status.get(k, 0) for k in ("x", "y", "angle")}
            else:
                pose = None
            scan = self._scan if self._scan and now - self._scan_time < STALE_AFTER_S else None
            return self._status, pose, pose_is_live, scan, self._error


# =============================================================================
# Drawing
# =============================================================================

def draw_map(ax, m: dict, params: dict) -> None:
    """Draw the parts of the map that don't change."""
    points = m.get("normalPosList") or []
    if points:
        ax.scatter([p["x"] for p in points], [p["y"] for p in points], s=0.3, c=COLOR_MAP, label="map")

    for area in m.get("advancedAreaList") or []:
        poly = area.get("posGroup") or []
        if len(poly) < 3:
            continue
        xs = [p["x"] for p in poly] + [poly[0]["x"]]
        ys = [p["y"] for p in poly] + [poly[0]["y"]]
        color = "red" if area.get("className") == "ForbiddenArea" else "orange"
        ax.fill(xs, ys, color=color, alpha=0.15)
        ax.plot(xs, ys, color=color, lw=1)
        ax.text(xs[0], ys[0], area.get("instanceName", ""), color=color, fontsize=8)

    for path in m.get("advancedCurveList") or []:
        xy = path_points(path, params)
        ax.plot([p[0] for p in xy], [p[1] for p in xy], color=COLOR_PATH, lw=1.5)

    for st in m.get("advancedPointList") or []:
        x, y, d = st["pos"]["x"], st["pos"]["y"], st.get("dir", 0)
        ax.plot(x, y, "s", color=COLOR_STATION, ms=7)
        ax.annotate("", xy=(x + 0.4 * math.cos(d), y + 0.4 * math.sin(d)), xytext=(x, y),
                    arrowprops=dict(arrowstyle="->", color=COLOR_STATION))
        ax.text(x + 0.1, y + 0.15, st.get("instanceName", ""), color=COLOR_STATION, fontsize=9)

    for r in m.get("reflectorList") or []:
        ax.plot(r["x"], r["y"], "*", color=COLOR_REFLECTOR, ms=8)

    h = m.get("header", {})
    if h.get("minPos") and h.get("maxPos") and h["minPos"] != h["maxPos"]:
        ax.set_xlim(h["minPos"]["x"] - 1, h["maxPos"]["x"] + 1)
        ax.set_ylim(h["minPos"]["y"] - 1, h["maxPos"]["y"] + 1)
    ax.autoscale(False)


class RobotLayer:
    """The parts that change: robot outline, lidar points and status text."""

    def __init__(self, ax, client: OnixClient):
        self.client = client
        dims = client.params.get("robot_dimensions", {})
        self.length, self.width = dims.get("width", 0.85), dims.get("height", 0.75)
        self.lidar = ax.scatter([], [], s=3, c=COLOR_LIDAR, label="lidar", zorder=4)
        self.body, = ax.plot([], [], color=COLOR_ROBOT, lw=2, zorder=5)
        self.heading, = ax.plot([], [], color=COLOR_ROBOT, lw=2, zorder=5)
        self.title = ax.set_title("connecting...", fontsize=10, loc="left")

    def update(self, live: LiveData) -> None:
        status, pose, pose_is_live, scan, error = live.snapshot()
        lines = []

        if status:
            s = self.client.get_summary(status)
            lines.append(f"Battery {s['battery_percent']}%   e-stop {s['emergency_stop']}   "
                         f"errors: {s['error_level']}   localization {s['localization_percent']}%")

        if pose:
            self._draw_robot(pose)
            note = "" if pose_is_live else "   [not localized: position may be 0]"
            lines.append(f"Robot ({pose['x']:.2f}, {pose['y']:.2f}, "
                         f"{math.degrees(pose['angle']):.0f}°){note}")

        points = self.client.lidar_to_points(scan, pose) if scan else []
        self.lidar.set_offsets(points or [[math.nan, math.nan]])
        lines.append(f"Lidar: {len(points)} points" if scan
                     else "Lidar: no data from the robot (check get_errors())")

        if error and not status:
            lines.append(f"Connection problem: {error}")
        self.title.set_text("\n".join(lines))

    def _draw_robot(self, pose: dict) -> None:
        c, s = math.cos(pose["angle"]), math.sin(pose["angle"])
        hl, hw = self.length / 2, self.width / 2
        corners = [(hl, hw), (hl, -hw), (-hl, -hw), (-hl, hw), (hl, hw)]
        self.body.set_data([pose["x"] + x * c - y * s for x, y in corners],
                           [pose["y"] + x * s + y * c for x, y in corners])
        self.heading.set_data([pose["x"], pose["x"] + self.length * 0.7 * c],
                              [pose["y"], pose["y"] + self.length * 0.7 * s])


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Live view of the ONIX robot")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"robot address (default {DEFAULT_HOST})")
    ap.add_argument("--save", metavar="FILE", help="save one picture instead of opening a window")
    ap.add_argument("--wait", type=float, default=3.0, help="seconds to collect data before --save")
    args = ap.parse_args()

    import matplotlib
    if args.save:
        matplotlib.use("Agg")  # no window needed
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    client = OnixClient(args.host)
    print("Loading live map...")
    m = client.get_live_map()["data"]
    info = client.summarize_map(m)
    print(f"Map '{info['name']}': {info['points']} points, {info['stations']} stations, "
          f"{info['paths']} paths, {info['areas']} areas")

    live = LiveData(client)
    live.start()

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    draw_map(ax, m, client.params)
    robot = RobotLayer(ax, client)
    ax.legend(loc="upper right", markerscale=6, fontsize=8)

    if args.save:
        time.sleep(args.wait)
        robot.update(live)
        fig.savefig(args.save, dpi=120, bbox_inches="tight")
        print("Saved", args.save)
        return

    # Keep a reference to the animation, or matplotlib stops it.
    animation = FuncAnimation(fig, lambda _frame: robot.update(live),
                              interval=REFRESH_MS, cache_frame_data=False)
    plt.show()
    del animation


if __name__ == "__main__":
    main()