"""
joint_mapping.py
----------------
Your JointMap: real Dobot servo angles <-> visual/model angles that go into
the DH table (kinematics.build_dh_params).
"""

import numpy as np


class JointMap:
    """
    Maps real servo angles (what the Dobot reports) <-> visual/model angles
    (what goes into the DH table).

        visual = direction * (servo - home_servo)
        servo  = home_servo + direction * visual

    At the home pose every visual angle is 0.
    direction[i] = +1 -> same rotation sense as the servo
    direction[i] = -1 -> flipped
    """

    def __init__(self, home_servo_deg, direction=(1, 1, 1, 1, 1, 1)):
        self.home_servo_deg = np.asarray(home_servo_deg, dtype=float)
        self.direction = np.asarray(direction, dtype=float)
        assert self.home_servo_deg.shape == (6,)
        assert self.direction.shape == (6,)
        assert np.all(np.abs(self.direction) == 1), "direction must be +1 or -1"

    def servo_to_visual(self, servo_deg):
        """servo angles [deg] -> visual angles [rad] for the DH table"""
        servo = np.asarray(servo_deg, dtype=float)
        return np.radians(self.direction * (servo - self.home_servo_deg))

    def visual_to_servo(self, visual_rad):
        """visual angles [rad] -> servo angles [deg] to command the robot"""
        visual = np.asarray(visual_rad, dtype=float)
        return self.home_servo_deg + self.direction * np.degrees(visual)


# ----------------------------------------------------------------------
# EDIT HERE
# ----------------------------------------------------------------------
joint_map = JointMap(
    home_servo_deg=[-180, 0, -90, 0, 90, 90],   # j1..j6 at your home pose
    direction=[1, 1, 1, 1, 1, 1],               # set any to -1 to flip that joint
)
