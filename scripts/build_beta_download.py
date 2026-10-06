"""Assemble the single beta download: Claude packages and the Codex installer.

There is one download rather than one per assistant, because the two installs
share a single memory store and a tester who runs both should not have to work
out that they belong together. It is generated, never edited by hand: the old
hand-zipped download kept its own copy of the Claude bundle and was still
handing testers a build whose defects had been fixed weeks earlier.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_claude_packages import VERSION, _write_archive, build  # noqa: E402


INSTALLER_ZIP_NAME = "MemorySafe-Beta-Installer-macOS-Intel.zip"


def _build_codex_installer(root: Path, staging: Path) -> Path:
    script = root / "scripts" / "build_macos_installer.sh"
    subprocess.run(
        ["/bin/zsh", str(script), str(staging)],
        check=True,
        capture_output=True,
        text=True,
    )
    installer = staging / INSTALLER_ZIP_NAME
    if not installer.is_file():
        raise RuntimeError("The macOS installer did not produce its archive.")
    return installer


def build_download(root: Path, output_dir: Path) -> tuple[Path, Path]:
    folder = output_dir / f"MemorySafe-Beta-{VERSION}"
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "claude").mkdir(parents=True)
    (folder / "codex").mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="memorysafe-download-") as temporary:
        staging = Path(temporary)
        code_zip, desktop_bundle = build(root, staging)
        shutil.copy2(desktop_bundle, folder / "claude" / desktop_bundle.name)
        shutil.copy2(code_zip, folder / "claude" / code_zip.name)
        installer = _build_codex_installer(root, staging)
        shutil.copy2(installer, folder / "codex" / installer.name)

    shutil.copy2(root / "plugin" / "INSTALL.md", folder / "INSTALL.md")
    archive = output_dir / f"MemorySafe-Beta-{VERSION}.zip"
    archive.unlink(missing_ok=True)
    _write_archive(folder, archive)
    return folder, archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder, archive = build_download(root, args.output_dir.expanduser().resolve())
    print(json.dumps({"download_folder": str(folder), "download_zip": str(archive)}, indent=2))


if __name__ == "__main__":
    main()
