"""Build the Claude Code zip and the Claude Desktop extension from plugin/.

The Claude Code zip feeds the beta download's claude/ folder; Claude Code on macOS,
Linux and Windows installs from the marketplace tree that build_plugin.py generates
instead, and that script reuses the Claude Desktop extension built here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path


VERSION = "0.4.17"

# Running the test suite leaves __pycache__ beside the launcher scripts, and copying the
# folder as-is shipped those bytecode files inside every bundle.
_NOT_SHIPPED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
# Everything under src/ ships. Code that must not -- the retired chat room, the hosted
# edition -- lives outside src/, where tests/test_public_boundary.py keeps it.
_SRC_NOT_SHIPPED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")


def _copy_core(root: Path, target: Path) -> None:
    shutil.copy2(root / "pyproject.toml", target / "pyproject.toml")
    shutil.copytree(
        root / "src",
        target / "src",
        ignore=_SRC_NOT_SHIPPED,
    )


def _draw_icon(path: Path) -> None:
    # 512 square: Claude Desktop recommends it, and the mcpb validator warns below it.
    scale = 2
    width = height = 256 * scale
    pixels = bytearray(bytes((5, 8, 13, 255)) * width * height)

    def color(value: str) -> tuple[int, int, int, int]:
        return tuple(bytes.fromhex(value.removeprefix("#"))) + (255,)  # type: ignore[return-value]

    def inside(x: int, y: int, box: tuple[int, int, int, int], radius: int) -> bool:
        left, top, right, bottom = box
        if not (left <= x < right and top <= y < bottom):
            return False
        cx = min(max(x, left + radius), right - radius - 1)
        cy = min(max(y, top + radius), bottom - radius - 1)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2

    def rounded(box: tuple[int, int, int, int], radius: int, value: str) -> None:
        box = tuple(v * scale for v in box)  # type: ignore[assignment]
        radius *= scale
        rgba = color(value)
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                if inside(x, y, box, radius):
                    offset = (y * width + x) * 4
                    pixels[offset : offset + 4] = bytes(rgba)

    rounded((4, 4, 252, 252), 54, "#13777C")
    rounded((8, 8, 248, 248), 50, "#05080D")
    rounded((48, 48, 128, 128), 14, "#19E4E9")
    rounded((152, 48, 208, 104), 14, "#0C79D8")
    rounded((48, 152, 104, 208), 14, "#0C79D8")
    rounded((128, 128, 208, 208), 14, "#68C4FF")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    raw = b"".join(b"\x00" + bytes(pixels[y * width * 4 : (y + 1) * width * 4]) for y in range(height))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _write_archive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            info = zipfile.ZipInfo.from_file(path, relative.as_posix())
            # Extensionless launchers (start, ensure_uv, capture_hook) are run through sh,
            # but keep them executable for anyone who unpacks the bundle by hand.
            if relative.as_posix().startswith("scripts/") and path.suffix in {".sh", ".cmd", ""}:
                info.external_attr = (0o100755 & 0xFFFF) << 16
            with path.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def build(root: Path, output_dir: Path) -> tuple[Path, Path]:
    source = root / "plugin"
    with tempfile.TemporaryDirectory(prefix="memorysafe-claude-") as temporary:
        temp = Path(temporary)

        code_root = temp / "memorysafe-claude-code"
        code_root.mkdir()
        _copy_core(root, code_root)
        shutil.copytree(source / ".claude-plugin", code_root / ".claude-plugin")
        shutil.copy2(source / ".mcp.json", code_root / ".mcp.json")
        shutil.copytree(source / "scripts", code_root / "scripts", ignore=_NOT_SHIPPED)
        shutil.copytree(source / "skills", code_root / "skills", ignore=_NOT_SHIPPED)
        # No README in the bundles. These are install payloads a host unpacks into its
        # own cache, not something anyone browses: what Claude Desktop shows the user is
        # manifest.json's long_description. The README is the marketplace landing page,
        # generated by build_plugin.py from plugin/README-template.md. Rendering it here
        # too would need build_plugin's constants, and build_plugin already imports this
        # module -- the import would be circular, and copying the constants across is the
        # duplication this whole change removes.
        shutil.copy2(source / "requirements.lock", code_root / "requirements.lock")
        _draw_icon(code_root / "assets" / "memorysafe-icon.png")

        desktop_root = temp / "memorysafe-claude-desktop"
        desktop_root.mkdir()
        _copy_core(root, desktop_root)
        shutil.copy2(source / "manifest.json", desktop_root / "manifest.json")
        shutil.copy2(source / "mcpb_server.py", desktop_root / "mcpb_server.py")
        # The desktop bundle launches through the same script as the Claude Code plugin,
        # so both packages share one runtime path instead of two that can drift apart.
        shutil.copytree(source / "scripts", desktop_root / "scripts", ignore=_NOT_SHIPPED)
        shutil.copy2(source / "requirements.lock", desktop_root / "requirements.lock")
        _draw_icon(desktop_root / "assets" / "memorysafe-icon.png")

        code_zip = output_dir / f"memorysafe-claude-code-{VERSION}.zip"
        desktop_bundle = output_dir / f"memorysafe-claude-desktop-{VERSION}.mcpb"
        _write_archive(code_root, code_zip)
        _write_archive(desktop_root, desktop_bundle)

    return code_zip, desktop_bundle


def main() -> None:
    # The tester download is assembled by scripts/build_beta_download.py, which
    # covers both assistants. Keeping a second Claude-only download here is how
    # one of them ends up stale.
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    outputs = build(root, args.output_dir.expanduser().resolve())
    print(json.dumps({"claude_code": str(outputs[0]), "claude_desktop": str(outputs[1])}))


if __name__ == "__main__":
    main()
