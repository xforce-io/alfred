#!/usr/bin/env python3
"""Launch the browser server in an independent process session."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: launch-server.py CWD LOG_FILE COMMAND")
    cwd, log_file, command = sys.argv[1:]
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab", buffering=0) as log:
        process = subprocess.Popen(
            ["bash", "-c", command],
            cwd=cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    print(process.pid)


if __name__ == "__main__":
    main()
