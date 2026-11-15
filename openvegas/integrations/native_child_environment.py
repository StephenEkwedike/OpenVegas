"""Restore external programs' linker paths without changing the frozen parent."""
from __future__ import annotations

import sys


def external_child_environment(env):
    result = dict(env)
    if sys.platform == 'linux' and getattr(sys, 'frozen', False):
        # PyInstaller saves the original path before prepending its bundle.
        # Preserve an explicitly empty original as well as caller-owned paths.
        if 'LD_LIBRARY_PATH_ORIG' in result:
            result['LD_LIBRARY_PATH'] = result['LD_LIBRARY_PATH_ORIG']
        else:
            result.pop('LD_LIBRARY_PATH', None)
    return result
