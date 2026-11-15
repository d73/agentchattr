"""Crash-safe file writes: write a temp file, fsync it, then atomically replace.

A plain write_text/open("w") truncates the target first, so a crash or power
loss mid-write leaves a partial file (and for settings, a silent reset to
defaults on the next start). os.replace swaps the finished file in one step.
"""

import os
import time
import uuid
from pathlib import Path


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On Windows os.replace can briefly fail if another process (antivirus,
        # indexer) has the target open, so retry a few times.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
