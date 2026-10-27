from __future__ import annotations

import argparse
import struct
from pathlib import Path


ICON_MEMBERS = (
    (b"icp4", "icon_16x16.png"),
    (b"ic11", "icon_16x16@2x.png"),
    (b"icp5", "icon_32x32.png"),
    (b"ic12", "icon_32x32@2x.png"),
    (b"ic07", "icon_128x128.png"),
    (b"ic13", "icon_128x128@2x.png"),
    (b"ic08", "icon_256x256.png"),
    (b"ic14", "icon_256x256@2x.png"),
    (b"ic09", "icon_512x512.png"),
    (b"ic10", "icon_512x512@2x.png"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a modern macOS ICNS file from an iconset.")
    parser.add_argument("iconset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    members: list[bytes] = []
    for member_type, filename in ICON_MEMBERS:
        payload = (args.iconset / filename).read_bytes()
        members.append(member_type + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(members)
    args.output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


if __name__ == "__main__":
    main()
