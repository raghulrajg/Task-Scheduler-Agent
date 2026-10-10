"""
modes.py
--------
Lets every program take --test / --real on its command line. Import and call
apply_cli_mode() BEFORE importing config, because config reads RUN_MODE when
it is first imported.

    import modes; modes.apply_cli_mode()
    import config
"""

import os
import sys
from typing import List, Optional


def apply_cli_mode(argv: Optional[List[str]] = None) -> None:
    argv = sys.argv if argv is None else argv
    for flag, mode in (("--test", "test"), ("--real", "real")):
        if flag in argv:
            argv.remove(flag)
            os.environ["RUN_MODE"] = mode
