"""The robot link layer: the only code in Autobot that touches the EBO SE.

`RobotLink` (link.py) is the in-process contract the brain calls. `NativeRobotLink` drives the real robot
through the TUTK native bridge; `MockRobotLink` fakes it for hardware-free dev. `make_link()` picks one
based on `Settings.robot_link`.
"""

from .link import RobotLink, make_link

__all__ = ["RobotLink", "make_link"]
