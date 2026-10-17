"""
perception.py
-------------
Object pose in the robot base frame from a camera detection:

    T_base_object = T_base_camera(q) @ T_CAMERA_DH_TO_OPTICAL @ T_camera_object

T_base_camera comes from the camera DH chain at the joint angles the arm had
*when the image was taken*, so the observation pose does not have to be home
(e.g. the base joint rotated 90 deg as in the workspace photo).
"""

import numpy as np

from .joint_mapping import joint_map
from .kinematics import fk

# Fixed rotation from the camera DH frame (camera_tranformation.py) to the
# camera's optical frame (OpenCV: z forward, x right, y down). The DH camera
# z axis already points along the view direction (straight down at home), so
# this defaults to identity. If detections come out rotated about the view
# axis, put the measured 4x4 here.
T_CAMERA_DH_TO_OPTICAL = np.eye(4)


def camera_T_object_from_rvec_tvec(rvec, tvec):
    """OpenCV solvePnP / ArUco (rvec, tvec in meters) -> 4x4 T_camera_object."""
    rvec = np.asarray(rvec, dtype=float).reshape(3)
    theta = np.linalg.norm(rvec)
    if theta < 1e-12:
        R = np.eye(3)
    else:
        k = rvec / theta
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = np.eye(3) + np.sin(theta) * K + (1 - np.cos(theta)) * K @ K
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(tvec, dtype=float).reshape(3)
    return T


def base_T_camera(servo_deg):
    """Camera pose in the base frame at the given Dobot servo angles [deg]."""
    T, _ = fk(joint_map.servo_to_visual(servo_deg), chain="camera")
    return T @ T_CAMERA_DH_TO_OPTICAL


def base_T_object(servo_deg, camera_T_object):
    """Object pose in the robot base frame."""
    return base_T_camera(servo_deg) @ np.asarray(camera_T_object, dtype=float)


def object_T_base(servo_deg, camera_T_object):
    """Robot base pose in the object frame (inverse of base_T_object)."""
    return np.linalg.inv(base_T_object(servo_deg, camera_T_object))


def table_z_from_marker(base_T_marker, marker_height_above_table=0.0):
    """
    Table surface height in the base frame from a detected marker. The table
    is horizontal, so its height in the base frame does not depend on the base
    joint angle; any marker lying flat on the table (or the object's marker,
    minus its height above the table) gives it directly.
    """
    return float(np.asarray(base_T_marker)[2, 3] - marker_height_above_table)
