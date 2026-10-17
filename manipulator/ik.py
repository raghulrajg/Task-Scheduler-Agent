"""
ik.py
-----
Analytic inverse kinematics for the tool tip (or flange / camera) and a
top-down pick planner that never lets the arm go below the table.

The chain has the UR-style geometry (joints 2, 3, 4 parallel, offsets d4,
d5, d6), so there are up to 8 closed-form solutions:

    shoulder (2) x wrist (2) x elbow (2)

Each one is checked with forward kinematics, mapped to Dobot servo angles
through joint_map, and kept only if:
  * the tool tip is at least TIP_CLEARANCE above the table plane, and
  * every joint origin / link end of the flange, tool and camera chains is
    at least LINK_CLEARANCE above it,
  * every servo angle is inside JOINT_LIMITS_SERVO_DEG.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .joint_mapping import joint_map
from .kinematics import A2, A3, D1, D4, CHAINS, dh_transform, fk, theta_offsets

# ----------------------------------------------------------------------
# EDIT HERE
# ----------------------------------------------------------------------
TIP_CLEARANCE = 0.005    # m, tool tip may never be lower than table + this
LINK_CLEARANCE = 0.04    # m, joints / link ends (roughly the link radius)
JOINT_LIMITS_SERVO_DEG = np.array([[-360, 360]] * 6, dtype=float)  # set your real limits
MAX_STEP_DEG = 30.0      # max joint change between two pick waypoints (no branch flips)

FK_TOLERANCE = 1e-6


@dataclass
class IKSolution:
    servo_deg: np.ndarray            # command these on the Dobot
    visual_rad: np.ndarray           # DH visual angles
    branch: tuple                    # (shoulder, wrist, elbow) signs
    fk_error: float                  # |FK(q) - target| (position m + rotation)
    min_tip_z: float                 # lowest tool tip z, base frame
    min_link_z: float                # lowest joint / link end z, base frame
    valid: bool = True
    reasons: List[str] = field(default_factory=list)


def _wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def _analytic_ik(T, chain, q6_hint=0.0):
    """All closed-form DH thetas reaching T with the given chain."""
    d5, d6, _ = CHAINS[chain]
    R, p = T[:3, :3], T[:3, 3]
    z6 = R[:, 2]
    p5 = p - d6 * z6

    r = np.hypot(p5[0], p5[1])
    if r < abs(D4):
        return []
    phi = np.arctan2(p5[1], p5[0])
    psi = np.arcsin(D4 / r)

    out = []
    for s_sh, q1 in ((1, phi + psi), (-1, phi + np.pi - psi)):
        z1 = np.array([np.sin(q1), -np.cos(q1), 0.0])
        c5 = np.clip(-z6 @ z1, -1.0, 1.0)
        v = R.T @ z1                                  # = (s5 c6, -s5 s6, -c5)
        for s_wr in (1, -1):
            q5 = s_wr * np.arccos(c5)
            s5 = np.sin(q5)
            q6 = q6_hint if abs(s5) < 1e-9 else np.arctan2(-v[1] / s5, v[0] / s5)

            T01 = dh_transform(np.pi / 2, 0.0, D1, q1)
            T45 = dh_transform(np.pi / 2, 0.0, d5, q5)
            T56 = dh_transform(0.0, 0.0, d6, q6)
            T14 = np.linalg.inv(T01) @ T @ np.linalg.inv(T45 @ T56)
            x, y = T14[0, 3], T14[1, 3]               # frame-3 origin = frame-4 origin - d4 z1
            c3 = (x * x + y * y - A2 * A2 - A3 * A3) / (2 * A2 * A3)
            if abs(c3) > 1.0 + 1e-9:
                continue
            c3 = np.clip(c3, -1.0, 1.0)
            for s_el in (1, -1):
                q3 = s_el * np.arccos(c3)
                q2 = np.arctan2(y, x) - np.arctan2(A3 * np.sin(q3), A2 + A3 * np.cos(q3))
                q4 = np.arctan2(T14[1, 0], T14[0, 0]) - q2 - q3
                out.append(((s_sh, s_wr, s_el), np.array([q1, q2, q3, q4, q5, q6])))
    return out


def _pose_error(A, B):
    return float(np.linalg.norm(A[:3, 3] - B[:3, 3]) + np.linalg.norm(A[:3, :3] - B[:3, :3]))


def _servo_near(servo_deg, ref_deg):
    """Pick servo + k*360 per joint: inside the limits and closest to ref."""
    out = np.empty(6)
    for i in range(6):
        lo, hi = JOINT_LIMITS_SERVO_DEG[i]
        cands = [servo_deg[i] + 360.0 * k for k in range(-2, 3)]
        cands = [c for c in cands if lo - 1e-6 <= c <= hi + 1e-6]
        out[i] = min(cands, key=lambda c: abs(c - ref_deg[i])) if cands else np.nan
    return out


def clearance(visual_rad, table_z):
    """(lowest tool tip z, lowest joint/link-end z) over all three chains."""
    _, tool = fk(visual_rad, "tool")
    _, cam = fk(visual_rad, "camera")
    _, flange = fk(visual_rad, "flange")
    tip_z = tool[-1][2, 3]
    # frame 1 is the fixed shoulder on the base; frames 2.. move
    links = [T[2, 3] for T in flange[1:]] + [T[2, 3] for T in tool[1:-1]] + [T[2, 3] for T in cam[1:]]
    return tip_z, min(links)


def solve_ik(T_target, table_z, chain="tool", current_servo_deg=None,
             tip_clearance=TIP_CLEARANCE, link_clearance=LINK_CLEARANCE, return_all=False):
    """
    T_target: 4x4 pose of the chain end (tool tip by default) in the base frame.
    table_z : table surface height in the base frame (m).
    Returns IKSolution list, valid ones first, each group sorted by how far the
    joints move from current_servo_deg (home if not given).
    """
    T_target = np.asarray(T_target, dtype=float)
    ref = joint_map.home_servo_deg if current_servo_deg is None else np.asarray(current_servo_deg, float)
    q6_hint = joint_map.servo_to_visual(ref)[5] + theta_offsets(chain)[5]

    sols = []
    for branch, theta in _analytic_ik(T_target, chain, q6_hint):
        visual = _wrap(theta - theta_offsets(chain))
        T_fk, _ = fk(visual, chain)
        err = _pose_error(T_fk, T_target)
        servo = _servo_near(joint_map.visual_to_servo(visual), ref)
        tip_z, link_z = clearance(visual, table_z)
        s = IKSolution(servo, visual, branch, err, tip_z, link_z)
        if err > FK_TOLERANCE:
            s.reasons.append(f"FK mismatch {err:.2e}")
        if np.any(np.isnan(servo)):
            s.reasons.append("joint limit")
        if tip_z < table_z + tip_clearance:
            s.reasons.append(f"tool tip {1000 * (tip_z - table_z):.1f} mm from table")
        if link_z < table_z + link_clearance:
            s.reasons.append(f"arm link {1000 * (link_z - table_z):.1f} mm from table")
        s.valid = not s.reasons
        sols.append(s)

    dist = lambda s: np.nansum(np.abs(s.servo_deg - ref))
    sols.sort(key=lambda s: (not s.valid, dist(s)))
    return sols if return_all else [s for s in sols if s.valid]


def _rot_z(yaw):
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


# Tool orientation at home: tool z points straight down at the table.
R_TOOL_HOME = fk(np.zeros(6), "tool")[0][:3, :3]


def top_down_pose(x, y, z, yaw):
    """Tool-tip pose pointing straight down, rotated by yaw about base z."""
    T = np.eye(4)
    T[:3, :3] = _rot_z(yaw) @ R_TOOL_HOME
    T[:3, 3] = (x, y, z)
    return T


def plan_pick(base_T_object, current_servo_deg, table_z=None, grasp_height=0.015,
              approach_height=0.10, steps=10):
    """
    Top-down pick of an object sitting on the table.

    base_T_object : object pose in the base frame (perception.base_T_object)
    table_z       : table height in the base frame; None -> the object frame
                    origin is taken to lie on the table surface
    grasp_height  : tool tip height above the table at the grasp (m)

    Returns dict with servo angles for pre_grasp / grasp / lift and every
    waypoint of the straight-down approach, or None if no safe plan exists.
    """
    base_T_object = np.asarray(base_T_object, dtype=float)
    if table_z is None:
        table_z = float(base_T_object[2, 3])
    x, y = base_T_object[0, 3], base_T_object[1, 3]
    yaw_obj = np.arctan2(base_T_object[1, 0], base_T_object[0, 0])
    z_grasp = table_z + grasp_height
    z_pre = z_grasp + approach_height
    zs = list(np.linspace(z_pre, z_grasp, steps + 1)) + list(np.linspace(z_grasp, z_pre, steps + 1))[1:]

    best = None
    for yaw in (yaw_obj, yaw_obj + np.pi):            # parallel gripper: 180 deg symmetric
        prev = np.asarray(current_servo_deg, dtype=float)
        path, motion = [], 0.0
        for z in zs:
            sols = solve_ik(top_down_pose(x, y, z, yaw), table_z, "tool", prev)
            if not sols:
                path = None
                break
            s = sols[0]
            step = np.abs(s.servo_deg - prev)
            if path and step.max() > MAX_STEP_DEG:   # branch flip mid-descent
                path = None
                break
            motion += step.sum()
            path.append(s)
            prev = s.servo_deg
        if path and (best is None or motion < best[0]):
            best = (motion, yaw, path)

    if best is None:
        return None
    _, yaw, path = best
    return {
        "table_z": table_z,
        "yaw": float(yaw),
        "pre_grasp": path[0].servo_deg,
        "grasp": path[steps].servo_deg,
        "lift": path[-1].servo_deg,
        "waypoints": [s.servo_deg for s in path],
        "min_tip_z": min(s.min_tip_z for s in path),
        "min_link_z": min(s.min_link_z for s in path),
    }
