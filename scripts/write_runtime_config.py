from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path


def write_mcp_wrapper(install_root: Path) -> Path:
    """Create the no-whitespace executable used by tunnel-client's stdio transport."""
    configured = os.environ.get("MEMORYSAFE_MCP_WRAPPER_PATH", "").strip()
    wrapper = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "bin" / "memorysafe-mcp-stdio"
    )
    wrapper = wrapper.resolve()
    if any(character.isspace() for character in str(wrapper)):
        raise ValueError("The MemorySafe MCP wrapper path cannot contain whitespace.")

    target = install_root / "scripts" / "run_mcp_service.sh"
    if not target.is_file():
        raise ValueError("MemorySafe's MCP launch script is missing.")

    wrapper.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = wrapper.with_suffix(".tmp")
    temporary.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        f"exec {shlex.quote(str(target))}\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o700)
    temporary.replace(wrapper)
    os.chmod(wrapper, 0o700)
    return wrapper


def write_config(install_root: Path) -> Path:
    root = install_root.expanduser().resolve()
    state = root / "runtime-state"
    secret = root / ".secrets" / "tunnel-runtime-key"
    tunnel_id_file = state / "tunnel-id"
    tunnel_id = tunnel_id_file.read_text(encoding="utf-8").strip()
    if not tunnel_id.startswith("tunnel_"):
        raise ValueError("MemorySafe does not have a valid tunnel ID yet.")
    if not secret.is_file() or secret.stat().st_size < 30:
        raise ValueError("MemorySafe does not have a complete runtime key yet.")

    config_dir = root / "config" / "tunnel-client"
    health_dir = state / "health"
    log_dir = state / "logs"
    for directory in (config_dir, health_dir, log_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(secret, 0o600)
    os.chmod(tunnel_id_file, 0o600)

    health_file = health_dir / "tunnel.url"
    health_file.unlink(missing_ok=True)
    config_path = config_dir / "memorysafe-runtime.json"

    # tunnel-client splits its command string on whitespace. Always generate a
    # stable no-space wrapper rather than relying on quoting or optional args support.
    mcp_entry = {
        "channel": "main",
        "command": str(write_mcp_wrapper(root)),
    }

    payload = {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://api.openai.com",
            "tunnel_id": tunnel_id,
            "api_key": f"file:{secret}",
        },
        "health": {
            "listen_addr": "127.0.0.1:0",
            "url_file": str(health_file),
        },
        "admin_ui": {"open_browser": False},
        "log": {
            "level": "info",
            "format": "json",
            "file": str(log_dir / "tunnel.log"),
        },
        "mcp": {
            "commands": [mcp_entry],
        },
    }
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    os.chmod(config_path, 0o600)
    return config_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", required=True, type=Path)
    args = parser.parse_args()
    print(write_config(args.install_root))


if __name__ == "__main__":
    main()
