"""
kinematics.py
-------------
Forward kinematics for the arm, reusing the dh_transform /
forward_kinematics_6dof from camera_tranformation.py, tooltip_transformation.py
and effective_transformation.py.

Those three files share one standard-DH chain and differ only in joints 5-6:

    chain    d5      d6      theta6 offset
    flange   0.12    0.088   0        (effective_transformation.py, no end effector)
    tool     0.1965  0.285   +90 deg  (tooltip_transformation.py)
    camera   0.1965  0.138   +90 deg  (camera_tranformation.py)

All angles going in are *visual* angles in radians (0 at home), i.e. the
output of joint_map.servo_to_visual().
"""

import numpy as np


def dh_transform(alpha, a, d, theta):
    """
    Computes the 4x4 Homogeneous Transformation Matrix for a single joint
    given standard Denavit-Hartenberg parameters:
    - alpha : link twist (radians)
    - a     : link length (meters)
    - d     : link offset (meters)
    - theta : joint angle (radians)
    """
    ct = np.cos(theta)
    st = np.sin(theta)
    ca = np.cos(alpha)
    sa = np.sin(alpha)

    return np.array([
        [ct, -st * ca,  st * sa, a * ct],
        [st,  ct * ca, -ct * sa, a * st],
        [0,   sa,       ca,      d],
        [0,   0,        0,       1],
    ])


def forward_kinematics_6dof(dh_table):
    """
    dh_table: Nx4 numpy array where each row is [alpha, a, d, theta]

    Returns:
      T_effective : T_0^N (base to end-effector)
      transforms  : list of individual transforms T_{i-1}^{i}
      cumulative  : list of cumulative transforms T_0^{i}
    """
    T_effective = np.eye(4)
    transforms = []
    cumulative = []

    for row in dh_table:
        alpha, a, d, theta = row
        T_i = dh_transform(alpha, a, d, theta)
        T_effective = T_effective @ T_i
        transforms.append(T_i)
        cumulative.append(T_effective.copy())

    return T_effective, transforms, cumulative


# Shared joints 1-4 (meters)
D1, A2, A3, D4 = 0.2234, 0.28, 0.225, 0.1175

# Per-chain joints 5-6: (d5, d6, theta6 offset in rad)
CHAINS = {
    "flange": (0.12,   0.088, 0.0),
    "tool":   (0.1965, 0.285, np.radians(90)),
    "camera": (0.1965, 0.138, np.radians(90)),
}


def theta_offsets(chain="tool"):
    """DH theta = visual angle + offset, per joint."""
    _, _, off6 = CHAINS[chain]
    return np.array([0.0, np.pi / 2, np.pi / 2, np.pi / 2, np.pi / 2, off6])


def build_dh_params(q, chain="tool"):
    """
    q: 6 visual angles in radians (output of joint_map.servo_to_visual).
    Rows are [alpha, a, d, theta], same as your original tables.
    """
    d5, d6, _ = CHAINS[chain]
    theta = np.asarray(q, dtype=float) + theta_offsets(chain)
    return np.array([
        [np.pi / 2, 0.0, D1,  theta[0]],   # Joint 1
        [0,         A2,  0.0, theta[1]],   # Joint 2
        [0.0,       A3,  0.0, theta[2]],   # Joint 3
        [np.pi / 2, 0.0, D4,  theta[3]],   # Joint 4
        [np.pi / 2, 0.0, d5,  theta[4]],   # Joint 5
        [0,         0.0, d6,  theta[5]],   # Joint 6
    ])


def fk(q, chain="tool"):
    """Base -> chain end (flange / tool tip / camera) at visual angles q."""
    T, _, cumulative = forward_kinematics_6dof(build_dh_params(q, chain))
    return T, cumulative
