from __future__ import annotations

import argparse
import os
from pathlib import Path


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 2


def rotate_log(path: Path, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> bool:
    """Rotate a runtime log before a service opens it, keeping a small bounded history."""
    if max_bytes < 1 or backups < 1:
        raise ValueError("Log rotation limits must be positive.")
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except FileNotFoundError:
        return False

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    first_backup = path.with_name(f"{path.name}.1")
    temporary = first_backup.with_name(f"{first_backup.name}.tmp")
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - max_bytes))
        temporary.write_bytes(source.read(max_bytes))
    os.chmod(temporary, 0o600)
    temporary.replace(first_backup)
    path.write_bytes(b"")
    os.chmod(path, 0o600)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate one MemorySafe runtime log if needed.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--backups", type=int, default=DEFAULT_BACKUPS)
    args = parser.parse_args()
    rotate_log(args.path, args.max_bytes, args.backups)


if __name__ == "__main__":
    main()
