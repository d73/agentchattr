"""Crash-safe file writes: write a temp file, fsync it, then atomically replace.

A plain write_text/open("w") truncates the target first, so a crash or power
loss mid-write leaves a partial file (and for settings, a silent reset to
defaults on the next start). os.replace swaps the finished file in one step.
"""

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable
from pathlib import Path


def write_backup_once(path: Path, data: bytes) -> bool:
    """Publish a complete backup without replacing a previous backup."""
    if path.is_file():
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                temporary.rename(path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            if not path.is_file():
                raise
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def repair_corrupt_json(path: Path, reason: str, recovered: dict) -> None:
    """Save the original bytes and atomically replace the primary with recovered state."""
    backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
    temporary = None
    try:
        with path.open("rb") as source, tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".recovery", delete=False
        ) as destination:
            temporary = Path(destination.name)
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(backup)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    logging.getLogger(__name__).warning(
        "Invalid JSON state in %s (%s); preserved original at %s", path, reason, backup
    )
    atomic_write_text(path, json.dumps(recovered, indent=2) + "\n")


def atomic_write_text(path, text: str | Iterable[str], encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            if isinstance(text, str):
                f.write(text)
            else:
                f.writelines(text)
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
