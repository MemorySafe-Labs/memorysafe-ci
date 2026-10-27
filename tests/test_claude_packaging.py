from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import claude_launcher


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module



_no_real_window = None


def setUpModule() -> None:
    """Stop a first start from opening a real browser window during the run.

    _start_dashboard hands the window to a daemon thread that polls for the page and
    then shells out to xdg-open or open. A test that forgets to stop it leaves that
    thread running after it finishes, and the call lands inside whatever test is
    running seconds later. That is how
    test_a_dashboard_it_started_from_an_older_plugin_is_replaced failed on CI with
    Popen called twice, the second an xdg-open, while passing every other run.

    Stopping it per test works until the next test forgets. This cannot forget.
    Tests that assert on the window patch this again themselves.
    """
    global _no_real_window
    _no_real_window = patch.object(claude_launcher, "_open_the_window_in_the_background")
    _no_real_window.start()


def tearDownModule() -> None:
    if _no_real_window is not None:
        _no_real_window.stop()

class ClaudePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_both_hosts_name_an_extensionless_command(self) -> None:
        """One entry serves every platform because the command carries no extension.

        Claude Code resolves it through cross-spawn's which+PATHEXT lookup and Codex
        through program_resolver.rs, so both find start.cmd on Windows and run start
        via its shebang everywhere else. Adding an extension, or naming /bin/sh as the
        command again, silently removes Windows - the marketplace has no platform
        filter to fall back on ("Unknown field 'platforms'", claude plugin validate).
        """
        # The command names the launcher directly. The exec bit does survive - both hosts
        # copy the plugin into a versioned cache at mode 755, measured on 2026-09-17 against
        # Claude Code 2.1.274 and Codex 0.154.0 - so the old sh prefix was unnecessary there.
        # Codex still does not expand variables in "command", which is why its entry uses a
        # ./-relative path plus cwd rather than ${PLUGIN_ROOT}.
        plugin = Path(__file__).resolve().parents[1] / "plugin"
        claude = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
        server = claude["mcpServers"]["memorysafe"]
        self.assertEqual(server["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/start")
        self.assertEqual(server["args"], [])

        codex = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
        entry = codex["mcpServers"]["memorysafe"]
        self.assertEqual(entry["command"], "./scripts/start")
        self.assertEqual(entry["cwd"], "${PLUGIN_ROOT}")
        self.assertEqual(entry["type"], "stdio")

    def test_windows_launchers_ship_beside_their_posix_twins(self) -> None:
        plugin = Path(__file__).resolve().parents[1] / "plugin"
        for name in ("start", "start.cmd", "ensure_uv", "ensure_uv.cmd"):
            self.assertTrue((plugin / "scripts" / name).is_file(), name)

    def test_claude_code_plugin_is_named(self) -> None:
        plugin = json.loads(
            (self.root / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(plugin["name"], "memorysafe")

    def test_mcp_configs_carry_no_developers_machine_path(self) -> None:
        """A developer's absolute macOS path (/Users/someone/...) was once committed
        into a shipped mcp config and went out to every tester. A plugin carrying one is
        broken for everyone else, since that path exists on nobody else's machine. Check
        the raw source files, not just the built tree: the source is what a human edits,
        and the incident was a source-file edit, not a build-time regression.
        """
        plugin = Path(__file__).resolve().parents[1] / "plugin"
        for name in (".mcp.json", "mcp.json"):
            text = (plugin / name).read_text(encoding="utf-8")
            for needle in ("/Users/", "/home/", "C:\\Users\\"):
                self.assertNotIn(needle, text, f"{name} contains {needle}")

    def test_desktop_manifest_advertises_all_tools(self) -> None:
        manifest = json.loads((self.root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], "0.4")
        # The mcpb schema requires server.mcp_config.command even for runtimes whose
        # prose docs call mcp_config optional. The bundle launches through the same
        # script as the Claude Code plugin so there is one runtime path, not two.
        self.assertEqual(manifest["server"]["type"], "binary")
        self.assertEqual(manifest["server"]["entry_point"], "scripts/start")
        self.assertIn("command", manifest["server"]["mcp_config"])
        self.assertEqual(len(manifest["tools"]), 12)
        self.assertEqual(
            {tool["name"] for tool in manifest["tools"]},
            {
                "memorysafe_remember",
                "memorysafe_find",
                "memorysafe_forget",
                "memorysafe_protect",
                "memorysafe_explain",
                "memorysafe_review_conflicts",
                "memorysafe_resolve_conflict",
                "memorysafe_restore",
                "memorysafe_doctor",
                "memorysafe_health",
                "memorysafe_set_auto_mode",
                "memorysafe_auto_capture",
            },
        )
        from memorysafe_chatgpt.server import server

        self.assertEqual(
            {tool["name"] for tool in manifest["tools"]},
            {tool.name for tool in asyncio.run(server.list_tools())},
            "The static extension manifest must advertise every runtime tool.",
        )

        from memorysafe_chatgpt.bootstrap_catalog import TOOLS

        self.assertEqual(
            manifest["tools"],
            [{"name": tool["name"], "description": tool["description"]} for tool in TOOLS],
            "Copy the descriptions from bootstrap_catalog.TOOLS; they drifted by hand before.",
        )

    def test_launcher_starts_dashboard_only_when_needed(self) -> None:
        with patch.object(claude_launcher, "_dashboard_is_running", return_value=False), patch(
            "memorysafe_chatgpt.claude_launcher.subprocess.Popen"
        ) as popen, tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", {"MEMORYSAFE_STATE_DIR": temporary}, clear=False
        ):
            claude_launcher._start_dashboard()
        popen.assert_called_once()

    def test_windows_launcher_survives_a_space_in_its_install_path(self) -> None:
        """Claude Desktop installs under "...\\Claude Extensions\\...", which has a space.

        The first fix quoted the path inside the args array and this test locked that
        shape in, so the suite went green while Windows stayed broken. A beta user
        then proved the shape does not launch: the host neither quotes the argument
        for us nor passes our quotes through. Three variants failed differently on a
        real machine - as-shipped quotes produced a literal \" in the command line,
        no quotes truncated the path at "Claude", doubled quotes gave a syntax error.

        So the path must not be an argument at all. Naming the script as the command
        spawns it as a program path instead of parsing it out of a command line.
        """
        manifest = json.loads((self.root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        win32 = manifest["server"]["mcp_config"]["platform_overrides"]["win32"]

        self.assertEqual(win32["command"], "${__dirname}\\scripts\\start.cmd")
        self.assertEqual(win32["args"], [])
        self.assertTrue((self.root / "plugin" / "scripts" / "start.cmd").is_file())

    def test_windows_data_folder_default_is_not_a_macos_path(self) -> None:
        """The field was required and pre-filled with a macOS-only path.

        On Windows that path cannot exist, so Claude Desktop refused to enable the
        extension at all - under a description telling the user to leave it alone.
        Both launchers pick the right per-platform folder when the value is empty.
        """
        manifest = json.loads((self.root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        field = manifest["user_config"]["memory_directory"]
        self.assertFalse(field.get("required", False))
        self.assertNotIn("default", field)
        self.assertNotIn("Library/Application Support", json.dumps(field))

    def test_shipped_links_do_not_point_at_the_private_beta_host(self) -> None:
        """That host answers 401, so every tester following the link hit a wall."""
        for relative in ("plugin/manifest.json", "plugin/.claude-plugin/plugin.json"):
            payload = json.loads((self.root / relative).read_text(encoding="utf-8"))
            self.assertNotIn("chatgpt.site", payload["homepage"], relative)
            self.assertTrue(payload["homepage"].startswith("https://"), relative)

    @unittest.skip("needs scripts/memorysafe_selfcheck.py and scripts/uninstall_macos.py, not written yet")
    def test_download_holds_both_assistants_and_is_generated(self) -> None:
        from scripts import build_beta_download
        from scripts.build_claude_packages import VERSION

        def fake_installer(root: Path, staging: Path) -> Path:
            """The real installer signs and zips 20 MB; the assembly is what matters."""
            archive = staging / build_beta_download.INSTALLER_ZIP_NAME
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("MemorySafe Beta Installer.app/Contents/Info.plist", "test")
                handle.writestr(
                    "MemorySafe Beta Installer.app/Contents/Resources/payload/runtime/x86_64/bin/python3",
                    "",
                )
                handle.writestr(
                    "MemorySafe Beta Installer.app/Contents/Resources/payload/runtime/arm64/bin/python3",
                    "",
                )
            return archive

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            build_beta_download, "_build_codex_installer", fake_installer
        ):
            folder, archive = build_beta_download.build_download(self.root, Path(temporary))
            expected = {
                "INSTALL.md",
                # Travels in the download, not only inside the install: a tester whose
                # install failed cannot run a diagnostic that the install was supposed
                # to provide.
                "memorysafe_selfcheck.py",
                # Detects which assistants exist and configures only those, so nobody
                # has to work out which of three files applies to them.
                "setup_assistants.py",
                "uninstall_macos.py",
                f"claude/memorysafe-claude-desktop-{VERSION}.mcpb",
                f"claude/memorysafe-claude-code-{VERSION}.zip",
                f"codex/{build_beta_download.INSTALLER_ZIP_NAME}",
            }
            with zipfile.ZipFile(archive) as outer:
                self.assertEqual(set(outer.namelist()), expected)
            # The nested bundle must come from this build, not a stale copy.
            with zipfile.ZipFile(folder / "claude" / f"memorysafe-claude-desktop-{VERSION}.mcpb") as inner:
                manifest = json.loads(inner.read("manifest.json"))
            self.assertEqual(manifest["version"], VERSION)

    def test_installer_inputs_live_in_the_repository(self) -> None:
        """The installer build derived these from the repository's parent, so they
        stayed behind when the project moved and it could no longer be built."""
        for relative in (
            "legal/MemorySafe_Beta_Terms_of_Use.md",
            "legal/MemorySafe_Beta_Privacy_Policy.md",
            "assets/memorysafe-icon.png",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)
        script = (self.root / "scripts" / "build_macos_installer.sh").read_text(encoding="utf-8")
        self.assertNotIn("PROJECT_ROOT", script)

    def test_tiktoken_is_pinned_to_an_exact_version_with_a_current_wheel(self) -> None:
        """tiktoken==0.11.0 has no wheel for Python 3.14, and building it from source
        needs a Rust toolchain nobody installing MemorySafe has.
        start.cmd's Python search, and start's, can legitimately land on 3.14 -- so
        a regression back to a wheel-less pin fails the runtime build with a Rust compiler
        error nobody asking for MemorySafe expects, not a clear dependency message.

        This does not hardcode the current pin: a `>=` or unbounded range could resolve to
        a bad version just as easily as an exact pin could name one directly, so the first
        thing checked is that the specifier is `==` at all. The floor is checked separately,
        against known-bad versions only, so a legitimate version bump doesn't need this test
        edited to match it.
        """
        project = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'"tiktoken([^"]*)"', project)
        self.assertIsNotNone(match, "no tiktoken dependency found in pyproject.toml")
        specifier = match.group(1)
        self.assertTrue(
            specifier.startswith("=="), f"tiktoken must be pinned to an exact version, got {specifier!r}"
        )
        version = specifier.removeprefix("==")
        known_bad = {"0.11.0"}  # no Python 3.14 wheel; source build needs Rust
        self.assertNotIn(version, known_bad, f"tiktoken=={version} has no Python 3.14 wheel")

    @unittest.skip("needs the Developer ID gate in scripts/build_macos_installer.sh, not written yet")
    def test_public_macos_installer_fails_closed_without_developer_id(self) -> None:
        """An ad-hoc signature must never be mistaken for a distributable build."""
        script = self.root / "scripts" / "build_macos_installer.sh"
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.pop("MEMORYSAFE_BUILD_MODE", None)
            environment.pop("MEMORYSAFE_DEVELOPER_ID_APPLICATION", None)
            completed = subprocess.run(
                ["/bin/zsh", str(script), temporary],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires MEMORYSAFE_DEVELOPER_ID_APPLICATION", completed.stderr)

    @unittest.skip("needs notarization in scripts/build_macos_installer.sh, not written yet")
    def test_macos_release_pipeline_signs_notarizes_and_staples(self) -> None:
        script = (self.root / "scripts" / "build_macos_installer.sh").read_text(encoding="utf-8")
        self.assertIn("--options runtime --timestamp", script)
        self.assertIn("notarytool submit", script)
        self.assertIn("stapler staple", script)
        self.assertIn("stapler validate", script)
        self.assertIn("spctl --assess", script)
        self.assertIn("UNNOTARIZED.zip", script)
        self.assertIn("Bundled Python for $ARCH is missing", script)

    @unittest.skip("needs build_beta_download.require_bundled_runtime, not written yet")
    def test_download_refuses_an_installer_without_python(self) -> None:
        """The 8 MB notarized zip had no runtime and still looked like a release."""
        from scripts.build_beta_download import require_bundled_runtime

        with tempfile.TemporaryDirectory() as temporary:
            brick = Path(temporary) / "MemorySafe-Beta-Installer-macOS-Universal.zip"
            with zipfile.ZipFile(brick, "w") as handle:
                handle.writestr("MemorySafe Beta Installer.app/Contents/Info.plist", "test")
            with self.assertRaises(RuntimeError) as raised:
                require_bundled_runtime(brick)
            self.assertIn("no bundled Python", str(raised.exception))

    def test_install_guide_leads_with_the_plugin_install(self) -> None:
        guide = (self.root / "plugin" / "INSTALL.md").read_text(encoding="utf-8")
        for text in (
            "claude plugin install memorysafe@memorysafe",
            "codex plugin add memorysafe@memorysafe",
            "releases/latest/download/memorysafe-claude-desktop.mcpb",
            "memorysafe migrate --apply",
            "You do not need Python",
            "twelve tools",
            # Every first-start download is named, or "after that, nothing leaves" is untrue.
            "tokenizer data",
        ):
            self.assertIn(text, guide)
        self.assertNotIn("--plugin-dir", guide)
        # Was: assertLess(index("claude plugin install"), index("## ChatGPT on the web")).
        # The ChatGPT route is no longer documented -- it is developer-mode only and can
        # never be listed, because listing needs a public HTTPS endpoint. The intent the
        # old assertion carried, that the plugin leads rather than being buried under a
        # download, now holds against the Claude Desktop extension instead.
        self.assertNotIn("## ChatGPT on the web", guide)
        self.assertLess(guide.index("claude plugin install"), guide.index("memorysafe-claude-desktop.mcpb"))
        # The Windows files ship as 0.4.0 too; only the installer differs.
        self.assertNotIn("0.3 installation", guide)

    def test_plugin_readme_keeps_its_promises(self) -> None:
        """Reads the rendered template rather than a checked-in README: both public
        READMEs are generated from it now, because two hand-kept copies drifted and
        lost the Windows migrate path. Rendered, not raw -- these promises are what a
        user reads, and the raw template still holds {plugin}@{marketplace}.
        """
        from scripts.build_plugin import (
            MARKETPLACE_NAME,
            MCPB_NAME,
            PLUGIN_NAME,
            PUBLIC_REPOSITORY,
            _read_readme_template,
        )
        from scripts.plugin_meta import project_version

        readme = _read_readme_template(self.root).format(
            version=project_version(),
            repository=PUBLIC_REPOSITORY,
            plugin=PLUGIN_NAME,
            marketplace=MARKETPLACE_NAME,
            mcpb=MCPB_NAME,
        )
        self.assertIn("claude plugin install memorysafe@memorysafe", readme)
        self.assertIn("codex plugin add memorysafe@memorysafe", readme)
        self.assertIn("Automatic capture is **disabled**", readme)
        self.assertIn("not anyone's billed usage", readme)
        self.assertIn("tokenizer data", readme)
        self.assertNotIn("--plugin-dir", readme)
        # The connector has no quota and evicts nothing: forget is its only removal;
        # the outcome table said Protect shielded memories from eviction and Skip saved space.
        self.assertNotIn("eviction", readme)
        self.assertNotIn("worth the space", readme)

    def test_every_plugin_manifest_agrees_on_the_version(self) -> None:
        from memorysafe_chatgpt.bootstrap_catalog import VERSION as CATALOG_VERSION
        from scripts.build_claude_packages import VERSION
        from scripts.plugin_meta import project_version

        self.assertEqual(
            {"pyproject": project_version(), "catalog": CATALOG_VERSION},
            dict.fromkeys(("pyproject", "catalog"), VERSION),
        )
        for relative in (
            "plugin/.claude-plugin/plugin.json",
            "plugin/plugin.json",
            "plugin/manifest.json",
        ):
            payload = json.loads((self.root / relative).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], VERSION, relative)
            self.assertNotIn("chatgpt.site", payload["homepage"], relative)
        with (self.root / "packaging" / "macos" / "Installer-Info.plist").open("rb") as handle:
            installer = plistlib.load(handle)
        self.assertEqual(installer["CFBundleShortVersionString"], VERSION)
        self.assertIn(
            f'"CFBundleShortVersionString": "{VERSION}"',
            (self.root / "scripts" / "install_macos.py").read_text(encoding="utf-8"),
        )
        # plugin/INSTALL.md hardcodes the version twice: the page title, and the "What
        # changed in X.Y.Z" heading git history shows gets renamed and rewritten at
        # every release (0.3.2 -> 0.4.0 in one commit) rather than accumulated as a
        # changelog -- so, like the title, it names the current version, not a fixed
        # historical one, and belongs in this same agreement check.
        install_guide = (self.root / "plugin" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn(f"# MemorySafe Beta {VERSION} — Installation", install_guide)
        self.assertIn(f"## What changed in {VERSION}", install_guide)

    def test_built_archives_have_expected_entrypoints(self) -> None:
        from scripts.build_claude_packages import build

        with tempfile.TemporaryDirectory() as temporary:
            code_zip, desktop_bundle = build(self.root, Path(temporary))
            with zipfile.ZipFile(code_zip) as archive:
                names = set(archive.namelist())
                self.assertIn(".claude-plugin/plugin.json", names)
                self.assertIn(".mcp.json", names)
                self.assertIn("scripts/bootstrap_server.py", names)
                self.assertIn("scripts/install_runtime.py", names)
                self.assertIn("src/memorysafe_chatgpt/server.py", names)
                self.assertIn("scripts/start", names)
                self.assertIn("scripts/runtime.env", names)
                self.assertIn("requirements.lock", names)
            with zipfile.ZipFile(desktop_bundle) as archive:
                names = set(archive.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("mcpb_server.py", names)
                self.assertIn("assets/memorysafe-icon.png", names)
                # The manifest's entry_point must actually be in the bundle.
                self.assertIn("scripts/bootstrap_server.py", names)
                self.assertIn("scripts/install_runtime.py", names)
                self.assertIn("scripts/start", names)
                self.assertIn("scripts/runtime.env", names)
                self.assertIn("requirements.lock", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_manifest_cannot_disagree_with_the_installer_about_the_store(self) -> None:
        """One database path, derived in one place.

        The manifest used to hardcode MEMORYSAFE_DB_PATH beside the installer's own
        copy and this test checked the two strings matched. They could still drift
        the moment either was edited, and a mismatch is invisible: each assistant
        keeps its own store and both appear to have forgotten what the other was
        told. Worse, the macOS value was hardcoded even though the user may choose a
        different folder, so choosing one split the store immediately.

        The manifest now declares only the install root; both launchers derive the
        database and state paths from it, so disagreement is not expressible.
        """
        manifest = json.loads((self.root / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        config = manifest["server"]["mcp_config"]
        blocks = [config["env"], *(o["env"] for o in config["platform_overrides"].values())]
        for env in blocks:
            self.assertNotIn("MEMORYSAFE_DB_PATH", env)
            self.assertNotIn("MEMORYSAFE_STATE_DIR", env)
            self.assertEqual(env["MEMORYSAFE_INSTALL_ROOT"], "${user_config.memory_directory}")

        for launcher, marker in (
            ("start", "MEMORYSAFE_DB_PATH:-"),
            ("start.cmd", 'if "%MEMORYSAFE_DB_PATH%"==""'),
        ):
            text = (self.root / "plugin" / "scripts" / launcher).read_text(encoding="utf-8")
            self.assertIn(marker, text, f"{launcher} must derive the database path")

    def test_no_backtick_capture_leads_with_a_quote(self) -> None:
        """A `for /f "usebackq" ... in (`command`)` runs its command through cmd /c,
        which applies its own quote-stripping rule when the command line begins with a
        literal double quote -- and mis-parses the rest. start.cmd:77 hit exactly this:
        `` `"!UV!" python find --managed-python ...` `` leads with the quoted variable,
        so on a real machine (where %LOCALAPPDATA%, and so !UV!, contains a space
        whenever the username does) MANAGED_PYTHON silently stayed empty and the
        launcher fell all the way through to the degraded server -- the exact failure
        the uv bootstrap exists to prevent. This is the same trap that cost four failed
        attempts in ensure_uv.cmd's checksum verification (see the incident comment
        there), left uncaught in the file next door because each per-task review only
        ever saw its own diff.

        CI cannot catch this by running the scripts: tests/e2e/first_run.py's temporary
        directories never contain a space, so this test reads the source instead. A safe
        capture leads with a bare word (`call`, `where`, ...); this asserts none of them
        lead with `"` instead.
        """
        pattern = re.compile(r"in\s*\(\s*`([^`]*)`\s*\)")
        checked = 0
        for name in ("start.cmd", "ensure_uv.cmd"):
            text = (self.root / "plugin" / "scripts" / name).read_text(encoding="utf-8")
            for command in pattern.findall(text):
                checked += 1
                self.assertFalse(
                    command.lstrip().startswith('"'),
                    f"{name}: backtick capture leads with a quote: `{command}`",
                )
        # A change that stopped matching any capture at all would make this test
        # vacuously pass; guard against that the same way the rest of this file does.
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()


class DegradedModeTests(unittest.TestCase):
    """When MemorySafe cannot start, it must still connect and say why.

    A failed runtime build used to exit non-zero. Claude Desktop renders that as
    "Server disconnected", every MemorySafe tool vanishes, and the tool that could
    have explained the failure is inside the process that did not start. A beta
    tester spent an evening reverse-engineering the manifest instead.
    """

    root = Path(__file__).resolve().parents[1]

    @property
    def script(self) -> Path:
        return self.root / "plugin" / "scripts" / "degraded_server.py"

    def _talk(self, messages: list[dict], script: Path | None = None, **env_extra) -> list[dict]:
        env = dict(os.environ)
        env.update({"MEMORYSAFE_DEGRADED_REASON": "no_python", "MEMORYSAFE_INSTALL_LOG": ""})
        env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, str(script or self.script)],
            input="\n".join(json.dumps(m) for m in messages) + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_it_completes_an_mcp_handshake(self) -> None:
        replies = self._talk([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        by_id = {r.get("id"): r for r in replies}
        self.assertIn("protocolVersion", by_id[1]["result"])
        self.assertEqual([t["name"] for t in by_id[2]["result"]["tools"]], ["memorysafe_doctor"])

    def test_a_notification_is_never_answered(self) -> None:
        """Replying to a notification is a protocol error and can drop the session."""
        replies = self._talk([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(replies, [])

    def test_the_doctor_says_what_to_do_without_jargon(self) -> None:
        replies = self._talk([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "memorysafe_doctor", "arguments": {}}},
        ])
        report = replies[0]["result"]["structuredContent"]
        self.assertFalse(report["memorysafe_working"])
        self.assertIn("Python", report["headline"])
        self.assertTrue(report["what_to_do"])
        self.assertIn("No memories have been lost", report["note"])

    def test_stdout_is_reconfigured_before_any_reply_is_written(self) -> None:
        """Python opens stdout with newline=None on Windows, which rewrites every "\\n"
        this process writes to "\\r\\n" -- corrupting the newline-delimited JSON-RPC
        stream this is the only channel for. A true reproduction would need a real \\r
        to appear on the wire, but os.linesep is already "\\n" on this POSIX box, so
        reconfigure(newline="") and the untouched default write identical bytes here --
        there is nothing to observe at the byte level on Linux either way. This instead
        asserts the thing the fix actually guarantees: reconfigure runs, and it runs
        before the loop writes its first reply, by recording the real call order
        in-process rather than reading back through an OS pipe that would hide it.
        """
        module = _load_script(
            "memorysafe_degraded_server_reconfigure_test", self.script
        )
        order: list[tuple[str, object]] = []

        class RecordingStdout:
            def reconfigure(self, **kwargs) -> None:
                order.append(("reconfigure", kwargs))

            def write(self, text: str) -> None:
                order.append(("write", text))

            def flush(self) -> None:
                pass

        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        with patch.object(module.sys, "stdout", RecordingStdout()), patch.object(
            module.sys, "stdin", iter([json.dumps(message) + "\n"])
        ), patch.dict(os.environ, {"MEMORYSAFE_DEGRADED_REASON": "no_python"}):
            module.main()

        self.assertEqual(order[0], ("reconfigure", {"newline": ""}))
        self.assertTrue(any(call == "write" for call, _ in order[1:]))

    def test_it_reassures_about_data_for_every_reason_code(self) -> None:
        for reason in (
            "no_python",
            "runtime_build_failed",
            "download_failed",
            "uv_checksum_mismatch",
            "install_root_unwritable",
            "something_unmapped",
        ):
            replies = self._talk(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "memorysafe_doctor", "arguments": {}}}],
                MEMORYSAFE_DEGRADED_REASON=reason,
            )
            report = replies[0]["result"]["structuredContent"]
            self.assertTrue(report["what_to_do"], reason)
            self.assertIn("No memories have been lost", report["note"], reason)

    def test_it_imports_nothing_from_the_package_it_is_replacing(self) -> None:
        """It runs when the runtime is missing, so it cannot depend on the runtime."""
        import ast

        tree = ast.parse(self.script.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("memorysafe_chatgpt", imported)
        self.assertNotIn("mcp", imported)
        self.assertTrue(imported <= set(sys.stdlib_module_names), sorted(imported))

    def test_every_launch_path_hands_over_to_it_instead_of_exiting(self) -> None:
        # The POSIX launcher execs the proxy, and the proxy starts limited mode when the
        # runtime build fails; Windows still does it from its own launcher.
        for relative in ("start.cmd", "bootstrap_server.py"):
            text = (self.root / "plugin" / "scripts" / relative).read_text(encoding="utf-8")
            self.assertIn("degraded_server.py", text, relative)
            self.assertIn("MEMORYSAFE_DEGRADED_REASON", text, relative)

    def test_each_setup_failure_names_its_own_cause(self) -> None:
        """A plugin's first start downloads uv, a Python and packages. Each way that can
        fail needs its own sentence, or every one of them reads as "reinstall"."""
        expected = {
            "download_failed": "download",
            "uv_checksum_mismatch": "checksum",
            "install_root_unwritable": "folder",
        }
        headlines = set()
        for reason, word in expected.items():
            replies = self._talk(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "memorysafe_doctor", "arguments": {}}}],
                MEMORYSAFE_DEGRADED_REASON=reason,
            )
            headline = replies[0]["result"]["structuredContent"]["headline"]
            self.assertIn(word, headline, reason)
            headlines.add(headline)
        self.assertEqual(len(headlines), len(expected))

    def test_explain_is_the_doctors_wording_for_every_reason(self) -> None:
        """The proxy's progress page shows a failed build through explain(). It must say
        exactly what limited mode's doctor says, or one failure reads two ways."""
        module = _load_script("memorysafe_degraded_explain_test", self.script)
        for reason in (
            "no_python",
            "runtime_build_failed",
            "download_failed",
            "uv_checksum_mismatch",
            "install_root_unwritable",
            "something_unmapped",
        ):
            environment = {"MEMORYSAFE_DEGRADED_REASON": reason, "MEMORYSAFE_INSTALL_LOG": "", "MEMORYSAFE_INSTALL_ROOT": ""}
            with patch.dict(os.environ, environment):
                report = module._diagnose()
            self.assertEqual((report["headline"], report["what_to_do"]), module.explain(reason), reason)

    def test_explain_hands_out_a_fresh_list(self) -> None:
        """_diagnose inserts into the list it gets back; a shared one would grow on every call."""
        module = _load_script("memorysafe_degraded_explain_copy_test", self.script)
        module.explain("no_python")[1].append("mutated")
        self.assertNotIn("mutated", module.explain("no_python")[1])

    def _diagnose_on_python(self, version: tuple, reason: str) -> dict:
        """_diagnose as it runs on an older interpreter, which this suite cannot start."""
        import importlib.util
        import types
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location("memorysafe_degraded_under_test", self.script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        old_sys = types.SimpleNamespace(version_info=version)
        environment = {"MEMORYSAFE_DEGRADED_REASON": reason, "MEMORYSAFE_INSTALL_LOG": "", "MEMORYSAFE_INSTALL_ROOT": ""}
        with patch.object(module, "sys", old_sys), patch.dict(os.environ, environment):
            return module._diagnose()

    def test_only_a_missing_python_blames_the_fallbacks_age(self) -> None:
        """In pending mode on macOS and Linux the fallback runs on the system python3,
        which is 3.9 from the Command Line Tools or 3.8 on older Ubuntu. The runtime comes
        from uv, so that age is irrelevant, yet every uv failure opened by telling the
        person their Python was too old."""
        old = (3, 9, 6, "final", 0)
        for reason in ("download_failed", "uv_checksum_mismatch", "install_root_unwritable", "runtime_build_failed"):
            report = self._diagnose_on_python(old, reason)
            self.assertFalse(any("older than" in action for action in report["what_to_do"]), (reason, report["what_to_do"]))
        report = self._diagnose_on_python(old, "no_python")
        self.assertIn("older than the 3.10", report["what_to_do"][0])

    def test_it_reports_the_version_of_the_code_beside_it(self) -> None:
        initialize = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            copy = scripts / "degraded_server.py"
            shutil.copy2(self.script, copy)
            catalog = Path(temporary) / "src" / "memorysafe_chatgpt" / "bootstrap_catalog.py"
            catalog.parent.mkdir(parents=True)
            catalog.write_text('VERSION = "9.9.9"\n')
            found = self._talk(initialize, script=copy)[0]["result"]["serverInfo"]["version"]
            catalog.unlink()
            missing = self._talk(initialize, script=copy)[0]["result"]["serverInfo"]["version"]
        self.assertEqual((found, missing), ("9.9.9", "unknown"))
