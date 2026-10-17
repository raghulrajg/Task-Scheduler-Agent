"""
manipulator
-----------
Kinematics for the 6-DOF arm subsystem (the "arm" in robots_metadata.json):

  kinematics.py    your dh_transform / forward_kinematics_6dof + the three DH
                   chains (flange, tooltip, camera)
  joint_mapping.py your JointMap (Dobot servo deg <-> DH visual rad)
  perception.py    camera detection -> object pose in the robot base frame
  ik.py            analytic IK for the tool tip, filtered so nothing goes
                   below the table plane, plus a top-down pick planner
"""

from .kinematics import CHAINS, dh_transform, forward_kinematics_6dof, fk
from .joint_mapping import JointMap, joint_map
from .perception import (base_T_object, object_T_base, camera_T_object_from_rvec_tvec,
                         table_z_from_marker)
from .ik import IKSolution, solve_ik, plan_pick

__all__ = [
    "CHAINS", "dh_transform", "forward_kinematics_6dof", "fk",
    "JointMap", "joint_map",
    "base_T_object", "object_T_base", "camera_T_object_from_rvec_tvec", "table_z_from_marker",
    "IKSolution", "solve_ik", "plan_pick",
]
