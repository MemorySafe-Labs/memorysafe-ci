from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path


SETUP_LABEL = "ca.memorysafe.beta.setup"
TUNNEL_LABEL = "ca.memorysafe.beta.tunnel"


def copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(payload, stream, sort_keys=True)
    temporary.replace(path)
    os.chmod(path, 0o644)


def migrate_database(source: Path, target: Path) -> bool:
    if not source.is_file() or target.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # `with sqlite3.connect(...) as x:` only commits or rolls back the transaction --
    # Connection.__exit__ never closes the connection. Both connections here leaked
    # on that pattern; see commit 0e0d903 (MemoryStore._session()) for the same
    # defect and its fix. contextlib.closing() adds the close, and the inner `with
    # original, installed:` keeps the same commit/rollback semantics as before.
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as installed:
        with original, installed:
            original.backup(installed)
    os.chmod(target, 0o600)
    return True


def copy_private_file(source: Path, target: Path) -> bool:
    if not source.is_file() or target.is_file():
        return False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target.parent, 0o700)
    os.chmod(target, 0o600)
    return True


def create_launcher_app(app_path: Path, install_root: Path, icon_source: Path) -> None:
    contents = app_path / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    # Keep the executable filename shell- and LaunchServices-safe; the visible app
    # name remains "MemorySafe Beta" through CFBundleDisplayName.
    executable = macos / "MemorySafeBeta"
    quoted_install_root = shlex.quote(str(install_root))
    executable.write_text(
        "#!/bin/zsh\n"
        "set -u\n"
        f"INSTALL_ROOT={quoted_install_root}\n"
        f"if ! /bin/launchctl kickstart -k gui/$(/usr/bin/id -u)/{SETUP_LABEL} >/dev/null 2>&1; then\n"
        "  /usr/bin/nohup \"$INSTALL_ROOT/scripts/run_setup_service.sh\" >>\"$INSTALL_ROOT/runtime-state/logs/setup.output.log\" 2>>\"$INSTALL_ROOT/runtime-state/logs/setup.error.log\" &\n"
        "fi\n"
        f"if ! /bin/launchctl kickstart -k gui/$(/usr/bin/id -u)/{TUNNEL_LABEL} >/dev/null 2>&1; then\n"
        "  /usr/bin/nohup \"$INSTALL_ROOT/scripts/run_tunnel_service.sh\" >>\"$INSTALL_ROOT/runtime-state/logs/tunnel-service.output.log\" 2>>\"$INSTALL_ROOT/runtime-state/logs/tunnel-service.error.log\" &\n"
        "fi\n"
        "\"$INSTALL_ROOT/scripts/open_panel.sh\"\n",
        encoding="utf-8",
    )
    os.chmod(executable, 0o755)
    icon_name = "MemorySafe.icns"
    if icon_source.is_file():
        shutil.copyfile(icon_source, resources / icon_name)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "MemorySafe Beta",
        "CFBundleExecutable": "MemorySafeBeta",
        "CFBundleIconFile": icon_name,
        "CFBundleIdentifier": "ca.memorysafe.beta",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "MemorySafe Beta",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.4.12",
        "CFBundleVersion": "5",
        "LSMinimumSystemVersion": "12.0",
    }
    write_plist(contents / "Info.plist", info)


def create_cli_launcher(install_root: Path) -> Path:
    """Install a stable command outside the private venv's implementation layout."""
    launcher = install_root / "bin" / "memorysafe"
    python = install_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError("MemorySafe's private Python runtime is missing.")
    launcher.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = launcher.with_suffix(".tmp")
    temporary.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        "SCRIPT_DIR=${0:A:h}\n"
        "INSTALL_ROOT=${SCRIPT_DIR:h}\n"
        "export MEMORYSAFE_INSTALL_ROOT=\"$INSTALL_ROOT\"\n"
        "exec \"$INSTALL_ROOT/.venv/bin/python\" -m memorysafe_chatgpt.cli \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o755)
    temporary.replace(launcher)
    os.chmod(launcher, 0o755)
    return launcher


def launch_agent_payload(label: str, executable: Path, install_root: Path, log_name: str) -> dict:
    log_dir = install_root / "runtime-state" / "logs"
    environment = {
        "MEMORYSAFE_INSTALL_ROOT": str(install_root),
        "MEMORYSAFE_STATE_DIR": str(install_root / "runtime-state"),
        # Not under the install root. Claude installs into a different root, and this
        # path is exactly what split the two assistants into separate memories.
        "MEMORYSAFE_DB_PATH": str(
            Path.home() / "Library" / "Application Support" / "MemorySafe" / "data" / "memorysafe.sqlite3"
        ),
        "MEMORYSAFE_RUNTIME_KEY_FILE": str(install_root / ".secrets" / "tunnel-runtime-key"),
        "MEMORYSAFE_TUNNEL_ID_FILE": str(install_root / "runtime-state" / "tunnel-id"),
        "MEMORYSAFE_HEALTH_URL_FILE": str(install_root / "runtime-state" / "health" / "tunnel.url"),
        "MEMORYSAFE_LEGAL_DIR": str(install_root / "legal"),
        "MEMORYSAFE_ORCHESTRA_PATH": str(install_root / "data" / "orchestra-chat.jsonl"),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    return {
        "Label": label,
        "ProgramArguments": [str(executable)],
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "ProcessType": "Background",
        "RunAtLoad": True,
        "ThrottleInterval": 30,
        "StandardErrorPath": str(log_dir / f"{log_name}.error.log"),
        "StandardOutPath": str(log_dir / f"{log_name}.output.log"),
        "WorkingDirectory": str(install_root),
    }


def run(
    command: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, env=environment)


def bootstrap_agent(plist_path: Path, label: str) -> None:
    domain = f"gui/{os.getuid()}"
    run(["/bin/launchctl", "bootout", f"{domain}/{label}"], check=False)
    # A label that was ever disabled — by an uninstall, or by a user stopping the
    # service — makes bootstrap fail with a bare "Input/output error" forever after,
    # and nothing here undid it. Reinstalling could not fix an install in that state.
    run(["/bin/launchctl", "enable", f"{domain}/{label}"], check=False)
    run(["/bin/launchctl", "bootstrap", domain, str(plist_path)])
    run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the MemorySafe private beta.")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument(
        "--install-root",
        type=Path,
        # One folder per app, named for the app — not for its release channel. The
        # install used to live in "MemorySafe Beta" while the memories lived in
        # "MemorySafe", and that split is what let Claude and Codex keep separate stores.
        default=Path.home() / "Library" / "Application Support" / "MemorySafe",
    )
    parser.add_argument("--applications-dir", type=Path, default=Path.home() / "Applications")
    parser.add_argument("--launch-agents-dir", type=Path, default=Path.home() / "Library" / "LaunchAgents")
    parser.add_argument("--migration-source", type=Path)
    parser.add_argument("--tunnel-id", default="")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--skip-dependencies", action="store_true")
    parser.add_argument("--skip-launch", action="store_true")
    parser.add_argument("--skip-open", action="store_true")
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    install_root = args.install_root.expanduser().resolve()
    # LaunchAgents outlive the shell that created them. A test or build run against a
    # temporary install root would write plists pointing into /tmp, the directory would
    # be cleaned up, and the services would fail at the next login with nothing to say
    # why — which is exactly what happened while developing this.
    ephemeral = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
    install_is_ephemeral = str(install_root).startswith(ephemeral)
    if install_is_ephemeral and not args.skip_launch:
        raise SystemExit(
            f"Refusing to register background services for {install_root}: it is a "
            "temporary directory and the services would break once it is removed. "
            "Pass --skip-launch to install there for testing."
        )
    if install_is_ephemeral and args.launch_agents_dir == Path.home() / "Library" / "LaunchAgents":
        # Skipping the bootstrap was not enough: the plists were still written, so a
        # test against a temporary root overwrote the real ones and the services pointed
        # into a directory that was about to be deleted. Redirect them into the
        # throwaway install instead of the user's LaunchAgents folder.
        args.launch_agents_dir = install_root / "LaunchAgents"
        args.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    if not (source_root / "src" / "memorysafe_chatgpt").is_dir():
        raise SystemExit("The installer payload is incomplete.")

    # Anyone who installed before the folder was named for the app has their install in
    # "MemorySafe Beta". Retire it rather than leaving two folders behind, which is what
    # made the two assistants keep separate memories in the first place.
    legacy_root = Path.home() / "Library" / "Application Support" / "MemorySafe Beta"
    if legacy_root.is_dir() and legacy_root != install_root:
        legacy_store = legacy_root / "data" / "memorysafe.sqlite3"
        if legacy_store.is_file() and not (install_root / "data" / "memorysafe.sqlite3").is_file():
            (install_root / "data").mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = legacy_store.with_name(legacy_store.name + suffix)
                if source.is_file():
                    shutil.copy2(source, install_root / "data" / source.name)
            print(f"Moved memories from {legacy_root.name}.", flush=True)
        retired = legacy_root.with_name("MemorySafe Beta (replaced)")
        shutil.rmtree(retired, ignore_errors=True)
        try:
            legacy_root.rename(retired)
            print(f"Previous install kept at {retired.name}.", flush=True)
        except OSError:
            pass

    print("Installing MemorySafe Beta…", flush=True)
    for directory in (
        install_root,
        install_root / ".secrets",
        install_root / "data",
        install_root / "runtime-state" / "health",
        install_root / "runtime-state" / "logs",
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    copy_tree(source_root / "src", install_root / "src")
    copy_tree(source_root / "legal", install_root / "legal")
    copy_tree(source_root / "scripts", install_root / "scripts")
    copy_tree(source_root / "bin", install_root / "bin")
    shutil.copyfile(source_root / "pyproject.toml", install_root / "pyproject.toml")
    for executable in (
        install_root / "scripts" / "run_mcp_service.sh",
        install_root / "scripts" / "run_setup_service.sh",
        install_root / "scripts" / "run_tunnel_service.sh",
        install_root / "scripts" / "open_panel.sh",
        install_root / "bin" / "tunnel-client",
    ):
        if executable.is_file():
            os.chmod(executable, 0o755)

    if args.migration_source:
        migration = args.migration_source.expanduser().resolve()
        migrate_database(
            migration / "data" / "memorysafe.sqlite3",
            install_root / "data" / "memorysafe.sqlite3",
        )
        copy_private_file(
            migration / ".secrets" / "tunnel-runtime-key",
            install_root / ".secrets" / "tunnel-runtime-key",
        )
    if args.tunnel_id:
        tunnel_file = install_root / "runtime-state" / "tunnel-id"
        if not tunnel_file.is_file():
            tunnel_file.write_text(args.tunnel_id.strip() + "\n", encoding="utf-8")
            os.chmod(tunnel_file, 0o600)

    # Copy the interpreter into the install before building the venv against it. A venv
    # records an absolute path to its base Python, and the bundled one lives inside the
    # installer app — so a venv built directly against it breaks the moment the user
    # moves the installer to the Bin, which everyone does.
    bundled_runtime = source_root / "runtime" / ("arm64" if platform.machine() == "arm64" else "x86_64")
    runtime_python = install_root / "runtime" / "bin" / "python3"
    if bundled_runtime.is_dir():
        if not runtime_python.is_file():
            copy_tree(bundled_runtime, install_root / "runtime")
        args.python = str(runtime_python)

    venv_python = install_root / ".venv" / "bin" / "python"
    if not args.skip_dependencies:
        cache_root = install_root / "runtime-state" / "cache"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        installer_environment = os.environ.copy()
        installer_environment["PIP_CACHE_DIR"] = str(cache_root / "pip")
        installer_environment["XDG_CACHE_HOME"] = str(cache_root)
        # Reusing whatever venv is already there quietly defeats the bundled runtime: an
        # upgrade kept a venv built on a Python from a cache directory, so clearing that
        # cache would have killed the install. Rebuild unless it is already based on the
        # interpreter that ships with MemorySafe.
        expected_home = str((install_root / "runtime" / "bin").resolve())
        venv_config = install_root / ".venv" / "pyvenv.cfg"
        venv_is_ours = False
        if venv_python.is_file() and venv_config.is_file():
            for line in venv_config.read_text().splitlines():
                if line.startswith("home") and line.split("=", 1)[1].strip() == expected_home:
                    venv_is_ours = True
                    break
        if not venv_is_ours:
            if (install_root / ".venv").exists():
                print("Rebuilding the runtime environment on the bundled Python.", flush=True)
                shutil.rmtree(install_root / ".venv", ignore_errors=True)
            run([args.python, "-m", "venv", str(install_root / ".venv")])
        run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--editable",
                str(install_root),
            ],
            environment=installer_environment,
        )
        run([str(venv_python), "-c", "import mcp, cryptography"])
        run(
            [str(venv_python), "-c", "import tiktoken; tiktoken.get_encoding('o200k_base')"],
            environment=installer_environment,
        )
    elif not venv_python.is_file():
        source_venv = source_root / ".venv"
        if source_venv.is_dir():
            shutil.copytree(source_venv, install_root / ".venv", dirs_exist_ok=True, symlinks=True)

    create_cli_launcher(install_root)

    sys.path.insert(0, str(install_root / "src"))
    from memorysafe_chatgpt.device import ensure_device_identity

    ensure_device_identity(install_root / "runtime-state")

    log_dir = install_root / "runtime-state" / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    setup_plist = args.launch_agents_dir.expanduser().resolve() / f"{SETUP_LABEL}.plist"
    tunnel_plist = args.launch_agents_dir.expanduser().resolve() / f"{TUNNEL_LABEL}.plist"
    write_plist(
        setup_plist,
        launch_agent_payload(
            SETUP_LABEL,
            install_root / "scripts" / "run_setup_service.sh",
            install_root,
            "setup",
        ),
    )
    write_plist(
        tunnel_plist,
        launch_agent_payload(
            TUNNEL_LABEL,
            install_root / "scripts" / "run_tunnel_service.sh",
            install_root,
            "tunnel-service",
        ),
    )

    # Put the app where people look for apps. /Applications is writable by admin users
    # without a password prompt; fall back to ~/Applications rather than failing or
    # asking for one. Either way Spotlight and Launchpad find it.
    applications_dir = args.applications_dir.expanduser().resolve()
    if args.applications_dir == Path.home() / "Applications":
        shared = Path("/Applications")
        if os.access(shared, os.W_OK):
            applications_dir = shared
    applications_dir.mkdir(parents=True, exist_ok=True)
    app_path = applications_dir / "MemorySafe Beta.app"
    # The pip download cache is build scratch, not part of the install. Leaving it
    # behind costs the user 19 MB for files that will never be read again.
    cache_dir = install_root / "runtime-state" / "cache" / "pip"
    if cache_dir.is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)

    create_launcher_app(app_path, install_root, source_root / "MemorySafe.icns")
    if Path("/usr/bin/xattr").is_file():
        run(["/usr/bin/xattr", "-cr", str(app_path)], check=False)
    if Path("/usr/bin/codesign").is_file():
        run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_path)],
            check=False,
        )

    if not args.skip_launch:
        bootstrap_agent(setup_plist, SETUP_LABEL)
        bootstrap_agent(tunnel_plist, TUNNEL_LABEL)

    # Configure whatever else is on this Mac, so the tester is not left deciding which
    # of three files applies to them. Never fatal: an absent assistant is normal.
    run(
        # Pass the resolved install root explicitly. This script takes it from
        # --install-root while setup_assistants read it from the environment, so the two
        # could disagree and a path from one end up recorded by the other.
        [
            str(args.python),
            str(source_root / "scripts" / "setup_assistants.py"),
            str(source_root),
            "--install-root",
            str(install_root),
            "--desktop-only",
        ],
        check=False,
    )
    if not args.skip_open:
        time.sleep(1)
        run([str(install_root / "scripts" / "open_panel.sh")], check=False)
    print("MemorySafe Beta is installed.", flush=True)


if __name__ == "__main__":
    main()
