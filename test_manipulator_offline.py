"""
test_manipulator_offline.py
---------------------------
Checks the manipulator kinematics with no robot attached:
  1. FK at home matches the original tooltip/camera/flange scripts
  2. analytic IK recovers random joint configurations (all three chains)
  3. a target below the table gets no solution
  4. camera detection -> object pose in base frame, at home and with the
     base joint rotated 90 deg
  5. a full top-down pick plan never takes the tool tip or any link below
     the table, verified with forward kinematics

Run: python3 test_manipulator_offline.py
"""

import numpy as np

from manipulator import (CHAINS, base_T_object, camera_T_object_from_rvec_tvec, fk,
                         joint_map, object_T_base, plan_pick, solve_ik)
from manipulator.ik import LINK_CLEARANCE, TIP_CLEARANCE, clearance
from manipulator.perception import base_T_camera

np.set_printoptions(precision=4, suppress=True)


def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def test_home_fk():
    expected = {
        "flange": [-0.345, -0.1175, 0.4154],
        "tool":   [-0.4215, -0.1175, 0.2184],
        "camera": [-0.4215, -0.1175, 0.3654],
    }
    for chain, p in expected.items():
        T, _ = fk(np.zeros(6), chain)
        assert np.allclose(T[:3, 3], p, atol=1e-4), (chain, T[:3, 3])
        assert np.allclose(T[:3, 2], [0, 0, -1]), (chain, "should point down at home")
    print("home FK ok (tool tip at home:", fk(np.zeros(6), "tool")[0][:3, 3], ")")


def test_ik_round_trip(n=500):
    rng = np.random.default_rng(0)
    for chain in CHAINS:
        for _ in range(n):
            q = rng.uniform(-np.pi, np.pi, 6)
            T, _ = fk(q, chain)
            sols = solve_ik(T, table_z=-10.0, chain=chain, return_all=True)
            assert sols and all(s.fk_error < 1e-6 for s in sols)
            assert min(np.abs(wrap(s.visual_rad - q)).max() for s in sols) < 1e-6
    print(f"IK round trip ok ({n} random poses x {len(CHAINS)} chains)")


def test_below_table_rejected():
    table_z = 0.0
    T, _ = fk(np.zeros(6), "tool")
    T[2, 3] = table_z - 0.01
    assert solve_ik(T, table_z) == []
    reasons = {r.split(" ")[0] for s in solve_ik(T, table_z, return_all=True) for r in s.reasons}
    assert "tool" in reasons
    print("target 10 mm below table rejected")


def test_object_pose():
    obj = np.eye(4)
    obj[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]   # yawed 90 deg
    obj[:3, 3] = [-0.40, -0.12, 0.02]
    home = joint_map.home_servo_deg
    rotated = home + np.array([90, 0, 0, 0, 0, 0])     # base joint at 90 deg
    for servo in (home, rotated):
        cam_T_obj = np.linalg.inv(base_T_camera(servo)) @ obj   # what the camera would see
        assert np.allclose(base_T_object(servo, cam_T_obj), obj)
        assert np.allclose(object_T_base(servo, cam_T_obj) @ obj, np.eye(4))
    rvec, tvec = [0.1, -0.2, 0.3], [0.05, -0.02, 0.35]
    T = camera_T_object_from_rvec_tvec(rvec, tvec)
    assert np.allclose(T[:3, :3] @ T[:3, :3].T, np.eye(3)) and np.allclose(T[:3, 3], tvec)
    print("base_T_object ok at home and with base joint at 90 deg")


def test_pick_plan():
    table_z = -0.05
    obj = np.eye(4)
    obj[:3, 3] = [-0.40, -0.12, table_z]
    plan = plan_pick(obj, joint_map.home_servo_deg, table_z=table_z)
    assert plan is not None
    for servo in plan["waypoints"]:
        q = joint_map.servo_to_visual(servo)
        tip_z, link_z = clearance(q, table_z)
        assert tip_z >= table_z + TIP_CLEARANCE - 1e-9
        assert link_z >= table_z + LINK_CLEARANCE - 1e-9
    T, _ = fk(joint_map.servo_to_visual(plan["grasp"]), "tool")
    assert np.allclose(T[:3, 3], [-0.40, -0.12, table_z + 0.015], atol=1e-6)
    assert np.allclose(T[:3, 2], [0, 0, -1], atol=1e-6)
    print("pick plan ok, grasp servo deg:", plan["grasp"],
          f"| lowest tip {1000 * (plan['min_tip_z'] - table_z):.1f} mm above table")


if __name__ == "__main__":
    test_home_fk()
    test_ik_round_trip()
    test_below_table_rejected()
    test_object_pose()
    test_pick_plan()
    print("ALL OK")
