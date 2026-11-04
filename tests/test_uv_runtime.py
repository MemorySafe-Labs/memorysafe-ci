"""The uv builder in plugin/scripts/install_runtime.py.

A fake uv records what it was asked to do and creates just enough of a venv for the
import check to run against this test's own interpreter, so the lock, staging and
publish logic is exercised without a network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

FAKE_UV = """#!{python}
import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps({{
        "argv": arguments,
        "python_dir": os.environ.get("UV_PYTHON_INSTALL_DIR"),
        "cache_dir": os.environ.get("UV_CACHE_DIR"),
    }}) + "\\n")
if arguments[0] == "venv":
    python = Path(arguments[-1]) / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(Path(os.environ["FAKE_VENV_PYTHON"]).read_text())
    python.chmod(0o755)
elif arguments[0] == "pip":
    sys.exit(int(os.environ.get("FAKE_UV_PIP_EXIT", "0")))
"""

# The staged venv's python. It runs the import check on this test's interpreter, but a
# tokenizer fetch only records where it would cache and then fails, as it does offline:
# the real interpreter has tiktoken and would otherwise reach the network.
FAKE_VENV_PYTHON = """#!/bin/sh
case "$*" in
  *get_encoding*)
    printf '%s\\n' "$TIKTOKEN_CACHE_DIR" >> "$FAKE_PREFETCH_LOG"
    sleep "${{FAKE_PREFETCH_SLEEP:-0}}"
    exit 1
    ;;
esac
exec {python} "$@"
"""

FAKE_ENSURE_UV = """#!/bin/sh
if [ -n "${FAKE_ENSURE_EXIT:-}" ]; then exit "$FAKE_ENSURE_EXIT"; fi
echo "$FAKE_UV"
"""


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "memorysafe_uv_runtime_test", ROOT / "plugin" / "scripts" / "install_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(os.name == "nt", "the fake uv/ensure_uv/venv-python doubles below are POSIX shell scripts run "
                                   "directly by their shebang, which Windows cannot do; install_runtime.py's "
                                   "Windows branch is covered on any OS by EnsureUvWindowsBranchTests below, and "
                                   "the real build is covered end to end by the windows-latest first-run job")
class UvRuntimeBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_builder()
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.plugin = base / "plugin"
        scripts = self.plugin / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "runtime.env").write_text("RUNTIME_KEY=abc123\nPYTHON_VERSION=3.12\nUV_VERSION=0.12.15\n")
        (scripts / "ensure_uv").write_text(FAKE_ENSURE_UV)
        (self.plugin / "requirements.lock").write_text("mcp==2.0.0 \\\n    --hash=sha256:" + "0" * 64 + "\n")
        self.uv = base / "fake-uv"
        self.uv.write_text(FAKE_UV.format(python=sys.executable))
        self.uv.chmod(0o755)
        self.log = base / "uv.log"
        venv_python = base / "fake-venv-python"
        venv_python.write_text(FAKE_VENV_PYTHON.format(python=sys.executable))
        self.prefetch_log = base / "prefetch.log"
        self.data = base / "data"
        self.environment = patch.dict(
            os.environ,
            {
                "FAKE_UV": str(self.uv),
                "FAKE_UV_LOG": str(self.log),
                "FAKE_VENV_PYTHON": str(venv_python),
                "FAKE_PREFETCH_LOG": str(self.prefetch_log),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _main(self, **extra: str) -> int:
        with patch.dict(os.environ, extra), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return self.module.main(
                ["--plugin-dir", str(self.plugin), "--data-root", str(self.data)]
            )

    def _calls(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def _record(self) -> dict:
        path = self.data.resolve() / "runtime" / ".abc123.progress.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _recording(self):
        """Wraps the builder's reporter so a test sees every step in the order it ran."""
        calls: list = []
        real = self.module._reporter

        def factory(data_root, key):
            inner = real(data_root, key)

            class Recording:
                def step(self, name):
                    calls.append(("step", name))
                    inner.step(name)

                def ready(self):
                    calls.append(("ready",))
                    inner.ready()

                def failed(self, exit_code):
                    calls.append(("failed", exit_code))
                    inner.failed(exit_code)

            return Recording()

        return calls, patch.object(self.module, "_reporter", factory)

    def test_the_build_records_each_step_as_it_starts(self) -> None:
        """For several minutes the first start answered only "try again in a minute". The
        progress page, the doctor and the setup reply all read this record now."""
        calls, recording = self._recording()
        with recording:
            self.assertEqual(self._main(), 0)
        self.assertEqual(
            calls,
            [
                ("step", "uv"),
                ("step", "python"),
                ("step", "packages"),
                ("step", "verify"),
                ("step", "tokenizer"),
                ("ready",),
            ],
        )
        record = self._record()
        self.assertEqual((record["state"], record["step"]), ("ready", "tokenizer"))

    def test_a_failed_download_is_recorded_at_the_step_it_stopped(self) -> None:
        self.assertEqual(self._main(FAKE_ENSURE_EXIT="3"), 3)
        record = self._record()
        self.assertEqual(
            (record["state"], record["step"], record["failure"]),
            ("failed", "uv", "uv_checksum_mismatch"),
        )

    def test_a_failed_install_is_recorded_as_a_build_failure(self) -> None:
        self.assertEqual(self._main(FAKE_UV_PIP_EXIT="1"), 1)
        record = self._record()
        self.assertEqual(
            (record["state"], record["step"], record["failure"]),
            ("failed", "packages", "runtime_build_failed"),
        )

    def test_an_unwritable_record_does_not_fail_the_build(self) -> None:
        """The record is for people watching; the exit code alone decides success."""
        (self.data / "runtime" / ".abc123.progress.json").mkdir(parents=True)
        self.assertEqual(self._main(), 0)
        self.assertTrue((self.data / "runtime" / "abc123" / "ready").is_file())

    def test_the_builder_finds_the_record_module(self) -> None:
        """Without it the build still runs, but nobody can see it: a silent regression."""
        from memorysafe_chatgpt import provisioning

        self.assertIs(self.module.provisioning, provisioning)
        self.assertEqual(self.module.READY_MARKER, provisioning.READY_MARKER)

    def test_builds_a_runtime_keyed_by_the_lock(self) -> None:
        self.assertEqual(self._main(), 0)
        data = self.data.resolve()
        runtime = data / "runtime" / "abc123"
        self.assertTrue((runtime / "bin" / "python").is_file())
        self.assertTrue((runtime / "ready").is_file())

        venv, pip = self._calls()
        self.assertEqual(
            venv["argv"][:6],
            ["venv", "--python", "3.12", "--managed-python", "--no-project", "--relocatable"],
        )
        self.assertEqual(pip["argv"][:2], ["pip", "install"])
        self.assertIn("--require-hashes", pip["argv"])
        self.assertEqual(pip["argv"][-2:], ["-r", str(self.plugin.resolve() / "requirements.lock")])
        self.assertEqual(venv["python_dir"], str(data / "python"))
        self.assertEqual(venv["cache_dir"], str(data / "cache" / "uv"))
        self.assertEqual(
            sorted(path.name for path in (data / "runtime").iterdir()),
            [".abc123.progress.json", "abc123"],
        )

    def test_the_build_fetches_the_tokenizer_into_the_data_root(self) -> None:
        """tiktoken fetched its encoding on first use into the system temp folder, and again
        whenever that was cleared, so "after the first start nothing leaves this computer"
        was untrue. The build fetches it once, into the folder the launcher points at."""
        self.assertEqual(self._main(), 0)
        self.assertEqual(
            self.prefetch_log.read_text().splitlines(),
            [str(self.data.resolve() / "cache" / "tiktoken")],
        )

    def test_a_failed_tokenizer_fetch_still_publishes_the_runtime(self) -> None:
        # Offline, token counts fall back to a local estimate; that is no reason to leave
        # someone without MemorySafe.
        self.assertEqual(self._main(), 0)
        self.assertTrue(self.prefetch_log.is_file())
        self.assertTrue((self.data / "runtime" / "abc123" / "ready").is_file())

    def test_a_tokenizer_fetch_that_hangs_does_not_hold_up_the_build(self) -> None:
        with patch.object(self.module, "TOKENIZER_FETCH_TIMEOUT_SECONDS", 0.5):
            self.assertEqual(self._main(FAKE_PREFETCH_SLEEP="5"), 0)
        self.assertTrue((self.data / "runtime" / "abc123" / "ready").is_file())

    def test_a_ready_runtime_is_not_rebuilt(self) -> None:
        self.assertEqual(self._main(), 0)
        calls = len(self._calls())
        self.assertEqual(self._main(), 0)
        self.assertEqual(len(self._calls()), calls)

    def test_a_checksum_failure_keeps_its_exit_code(self) -> None:
        self.assertEqual(self._main(FAKE_ENSURE_EXIT="3"), 3)
        self.assertFalse((self.data / "runtime" / "abc123").exists())

    def test_a_download_failure_keeps_its_exit_code(self) -> None:
        self.assertEqual(self._main(FAKE_ENSURE_EXIT="2"), 2)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_an_unwritable_data_root_is_reported_as_such(self) -> None:
        self.data.mkdir()
        self.data.chmod(0o500)
        try:
            self.assertEqual(self._main(), 4)
        finally:
            self.data.chmod(0o700)

    def test_a_failed_install_publishes_nothing(self) -> None:
        self.assertEqual(self._main(FAKE_UV_PIP_EXIT="1"), 1)
        self.assertEqual(
            sorted(path.name for path in (self.data / "runtime").iterdir()),
            [".abc123.progress.json"],
        )


class EnsureUvWindowsBranchTests(unittest.TestCase):
    """_ensure_uv's OS branch, isolated from UvRuntimeBuilderTests's POSIX-only shell
    doubles above so it runs -- and means something -- on every OS, windows-latest CI
    included, not just the Linux and macOS runners that class is limited to.
    """

    def setUp(self) -> None:
        self.module = _load_builder()

    def test_ensure_uv_runs_the_windows_script_on_nt(self) -> None:
        """_ensure_uv hardcoded /bin/sh, which does not exist on Windows at all."""
        recorded = {}

        def fake_run(command, **kwargs):
            recorded["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout="C:\\uv.exe\n", stderr="")

        # Built before os.name is patched: pathlib picks WindowsPath vs PosixPath from
        # os.name at construction time, and this process cannot instantiate a WindowsPath.
        plugin_dir = Path("C:\\plugin")
        data_root = Path("C:\\data")
        with patch.object(self.module.os, "name", "nt"), \
                patch.object(self.module.subprocess, "run", fake_run):
            self.module._ensure_uv(plugin_dir, data_root)
        self.assertIn("ensure_uv.cmd", str(recorded["command"][-1]))
        self.assertNotIn("/bin/sh", [str(part) for part in recorded["command"]])


if __name__ == "__main__":
    unittest.main()
