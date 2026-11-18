"""The pinned inputs of the plugin runtime, and the files the launcher reads them from.

plugin/scripts/start is POSIX sh: it cannot hash a file portably or parse TOML. So the
runtime key and the uv checksums are written into plugin/scripts/runtime.env and
plugin/scripts/uv-checksums and committed. tests/test_plugin_meta.py fails whenever the
committed files disagree with this module or with plugin/requirements.lock.

After changing a dependency or a pin, from the repository root:

    uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.12 --quiet -o plugin/requirements.lock
    .venv/bin/python scripts/plugin_meta.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12"
UV_VERSION = "0.12.15"
# Copied from the .sha256 files published with the uv release above. Never fetched at
# build time: a checksum downloaded from the same place as the binary proves nothing.
# The Windows pair was additionally cross-checked against Microsoft's WinGet manifest
# for astral-sh.uv 0.12.15, which lists 477BD99A... for x64 - an independent publisher.
UV_CHECKSUMS = {
    "aarch64-apple-darwin": "dc304b9ed1b24174572290fba60ac3f6fe63c73a671f0439e62a91375841964d",
    "x86_64-apple-darwin": "e9ca61775532368fe518ab03e7a354c7ecab8ccb3c7d941c775fcc4a362b801b",
    "aarch64-unknown-linux-musl": "93b801abb146e6431fb0434346a0162e65d3f0d1cd7360144d04c43488fd7f7d",
    "x86_64-unknown-linux-musl": "999c0c3da986953e508985c3932d283d2c62eb167b4f8d81e79f565e34104959",
    "aarch64-pc-windows-msvc": "a37c8e96cb1260488c8510b64c848533a3a82a2fdf9e905de7c2700ceebf6437",
    "x86_64-pc-windows-msvc": "477bd99a84e34891f2bd4c9152ddeb74e971accccbc59c0f0301f11f08a32d46",
}
LOCK = ROOT / "plugin" / "requirements.lock"
RUNTIME_ENV = ROOT / "plugin" / "scripts" / "runtime.env"
UV_CHECKSUMS_FILE = ROOT / "plugin" / "scripts" / "uv-checksums"


def project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def runtime_key(lock_bytes: bytes, python_version: str = PYTHON_VERSION) -> str:
    """Name of the runtime directory. It changes only when what is installed changes.

    The application code runs from the plugin folder, so a release that leaves the lock
    alone reuses the runtime every assistant already built.
    """

    digest = hashlib.sha256(lock_bytes + b"\npython=" + python_version.encode("ascii"))
    return digest.hexdigest()[:16]


def runtime_env_text(lock_bytes: bytes) -> str:
    return (
        f"RUNTIME_KEY={runtime_key(lock_bytes)}\n"
        f"PYTHON_VERSION={PYTHON_VERSION}\n"
        f"UV_VERSION={UV_VERSION}\n"
    )


def _archive_name(target: str) -> str:
    # uv ships Windows as a flat .zip and every other target as a nested .tar.gz.
    return "uv-%s.zip" % target if target.endswith("-pc-windows-msvc") else "uv-%s.tar.gz" % target


def uv_checksums_text() -> str:
    return "".join(
        f"{digest}  {_archive_name(target)}\n" for target, digest in sorted(UV_CHECKSUMS.items())
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or rewrite the plugin runtime files.")
    parser.add_argument("--write", action="store_true", help="Rewrite them from the pins and the lock.")
    args = parser.parse_args(argv)
    expected = {
        RUNTIME_ENV: runtime_env_text(LOCK.read_bytes()),
        UV_CHECKSUMS_FILE: uv_checksums_text(),
    }
    if args.write:
        for path, text in expected.items():
            path.write_text(text, encoding="utf-8")
        return 0
    stale = [
        path
        for path, text in expected.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]
    for path in stale:
        print(f"stale: {path.relative_to(ROOT)} (run scripts/plugin_meta.py --write)", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
