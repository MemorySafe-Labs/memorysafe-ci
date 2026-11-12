"""memorysafe migrate: remove the hand-written registrations the plugins replace.

~/.claude.json holds the user's whole Claude Code configuration and config.toml their
whole Codex configuration. Only the memorysafe entries may change, only on --apply,
only after a backup, and only for an assistant whose MemorySafe plugin is installed:
the plugin runtime is shared by every host, so its existence says nothing about whether
the host holding a manual entry has any other way to reach MemorySafe.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import migrate
from memorysafe_chatgpt.cli import main as cli_main


CODEX_BEFORE = """# Codex settings
model = "o4"

[mcp_servers.other]
command = "other"

[mcp_servers.memorysafe]
command = "/x/scripts/run_mcp_service.sh"
args = []
startup_timeout_sec = 120.0

[mcp_servers.memorysafe.env]
MEMORYSAFE_INSTALL_ROOT = "/x"

# Keep this comment with the table below
[profiles.work]
model = "o3"
"""

CODEX_AFTER = """# Codex settings
model = "o4"

[mcp_servers.other]
command = "other"


# Keep this comment with the table below
[profiles.work]
model = "o3"
"""

# A bracket-only line that is the tail of a multi-line array, not a table header.
# `[1, 2]` is valid TOML nested inside `args = [...]`, but a header regex that only
# checks for a leading `[` and trailing `]` cannot tell the two apart.
CODEX_MULTILINE_ARRAY = """# Codex settings
model = "o4"

[mcp_servers.other]
command = "other"

[mcp_servers.memorysafe]
command = "/x/scripts/run_mcp_service.sh"
args = [
  "a",
  [1, 2]
]

[profiles.work]
model = "o3"
"""

# A `[not a header]`-shaped line inside a multi-line string, not a table header.
CODEX_MULTILINE_STRING = '''# Codex settings
model = "o4"

[mcp_servers.other]
command = "other"

[mcp_servers.memorysafe]
command = "/x/scripts/run_mcp_service.sh"
notes = """
Not a header:
[not a header]
"""

[profiles.work]
model = "o3"
'''

# What `codex plugin add memorysafe@memorysafe` leaves in config.toml.
CODEX_PLUGIN = """
[plugins."memorysafe@memorysafe"]
enabled = true
"""


class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.home = base / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.root = base / "MemorySafe"
        (self.root / "data").mkdir(parents=True)
        self.claude = self.home / ".claude.json"
        self.codex = self.home / ".codex" / "config.toml"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _register_claude(self) -> None:
        self.claude.write_text(
            json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}, "memorysafe": {"command": "y"}}}, indent=2)
        )

    def _install_claude_plugin(self, enabled: bool | None = None) -> None:
        # The shape Claude Code writes: plugin ids as "name@marketplace" keys.
        plugins = self.home / ".claude" / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"memorysafe@memorysafe": [{"scope": "user"}]}})
        )
        if enabled is not None:
            (self.home / ".claude" / "settings.json").write_text(
                json.dumps({"enabledPlugins": {"memorysafe@memorysafe": enabled}})
            )

    def _register_both(self) -> None:
        """Both manual entries, beside both plugins: every tool is listed twice in each."""
        self._register_claude()
        self._install_claude_plugin()
        self.codex.write_text(CODEX_BEFORE + CODEX_PLUGIN)

    def _cli(self, *arguments: str) -> str:
        output = StringIO()
        argv = ["memorysafe", "--install-root", str(self.root), "migrate", *arguments]
        with patch("sys.argv", argv), patch("pathlib.Path.home", return_value=self.home), redirect_stdout(output):
            cli_main()
        return output.getvalue()

    def test_a_codex_entry_comes_with_the_warning_to_close_codex(self) -> None:
        """Codex rewrites config.toml from its own state.

        On this machine the same block came back twice, hours after being removed,
        which made it look as though MemorySafe was rewriting its own registration.
        A long-running Codex had simply saved its config again.
        """
        self._register_both()
        for arguments in ((), ("--apply",)):
            report = self._cli(*arguments)
            self.assertIn("Close Codex first", report, arguments)

    def test_no_codex_entry_means_no_such_warning(self) -> None:
        self._register_claude()
        self._install_claude_plugin()
        self.assertNotIn("Close Codex first", self._cli())

    def test_a_dry_run_changes_nothing_and_says_what_it_would(self) -> None:
        self._register_both()
        before = (self.claude.read_bytes(), self.codex.read_bytes())
        report = json.loads(self._cli("--json"))
        self.assertFalse(report["applied"])
        self.assertEqual(report["found"]["claude_code"]["registered"], True)
        self.assertEqual(report["found"]["claude_code"]["plugin_installed"], True)
        self.assertEqual(report["found"]["codex"]["registered"], True)
        self.assertEqual(report["found"]["codex"]["plugin_installed"], True)
        self.assertEqual(report["changed"], [])
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), before)
        self.assertFalse(list(self.home.rglob("*.memorysafe-backup")))

    def test_apply_removes_only_the_memorysafe_entries(self) -> None:
        self._register_both()
        report = json.loads(self._cli("--apply", "--json"))
        self.assertEqual(sorted(report["changed"]), sorted([str(self.claude), str(self.codex)]))
        claude = json.loads(self.claude.read_text())
        self.assertEqual(claude, {"theme": "dark", "mcpServers": {"other": {"command": "x"}}})
        self.assertEqual(self.codex.read_text(), CODEX_AFTER + CODEX_PLUGIN)

    def test_apply_backs_up_each_file_it_changes(self) -> None:
        self._register_both()
        self._cli("--apply")
        self.assertEqual(Path(f"{self.codex}.memorysafe-backup").read_text(), CODEX_BEFORE + CODEX_PLUGIN)
        self.assertIn("memorysafe", json.loads(Path(f"{self.claude}.memorysafe-backup").read_text())["mcpServers"])

    def test_apply_returns_the_migrated_files(self) -> None:
        """migrate.apply's own return value, not just the CLI's --json report that
        test_apply_removes_only_the_memorysafe_entries already covers, names both
        rewritten files."""
        self._register_both()
        self.claude.chmod(0o600)
        self.codex.chmod(0o640)
        self.assertEqual(sorted(migrate.apply(self.home)), sorted([str(self.claude), str(self.codex)]))

    @unittest.skipIf(os.name == "nt", "asserts live 0o600/0o640 stats on the rewritten config files; "
                                       "Windows returns 0o666 for every file regardless of what chmod "
                                       "set beforehand, since NTFS has no POSIX mode bits to narrow -- "
                                       "test_apply_returns_the_migrated_files above still runs on Windows "
                                       "and covers migrate.apply's return value there")
    def test_apply_keeps_each_files_permissions(self) -> None:
        """~/.claude.json holds the user's whole Claude Code configuration and can be 0600.
        The rewrite goes through a new file created with the umask's mode, which the final
        branch review found would leave it readable by other users after apply."""
        self._register_both()
        self.claude.chmod(0o600)
        self.codex.chmod(0o640)
        migrate.apply(self.home)
        self.assertEqual(stat.S_IMODE(self.claude.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.codex.stat().st_mode), 0o640)

    def test_a_codex_entry_without_the_codex_plugin_is_kept(self) -> None:
        """Only Claude Code has the plugin. The runtime it built is shared, but Codex's
        manual entry is still Codex's only way to reach MemorySafe. The final branch
        review found migrate removed it anyway, which would cut Codex off from the store."""
        self._register_claude()
        self._install_claude_plugin()
        self.codex.write_text(CODEX_BEFORE)
        report = json.loads(self._cli("--apply", "--json"))
        self.assertEqual(report["changed"], [str(self.claude)])
        self.assertEqual(report["found"]["codex"], {"config": str(self.codex), "registered": True, "plugin_installed": False})
        self.assertEqual(self.codex.read_text(), CODEX_BEFORE)
        self.assertFalse(Path(f"{self.codex}.memorysafe-backup").exists())

    def test_a_kept_entry_is_explained_and_a_removed_one_asks_for_a_restart(self) -> None:
        self._register_claude()
        self._install_claude_plugin()
        self.codex.write_text(CODEX_BEFORE)
        text = self._cli("--apply")
        self.assertIn("kept: the MemorySafe plugin for Codex is not installed", text)
        self.assertIn("Restart Claude Code", text)
        self.assertNotIn("Restart Codex", text)

    def test_a_dry_run_names_only_the_entries_it_would_remove(self) -> None:
        self.codex.write_text(CODEX_BEFORE)
        text = self._cli()
        self.assertIn("kept: the MemorySafe plugin for Codex is not installed", text)
        self.assertNotIn("would remove", text)
        self.assertNotIn("--apply", text)

    def test_a_claude_code_entry_with_its_plugin_installed_is_removed(self) -> None:
        self._register_claude()
        self._install_claude_plugin()
        self.assertTrue(migrate.claude_code_plugin_installed(self.home))
        self.assertEqual(migrate.apply(self.home), [str(self.claude)])
        self.assertNotIn("memorysafe", json.loads(self.claude.read_text())["mcpServers"])

    def test_a_claude_code_entry_without_its_plugin_is_kept(self) -> None:
        self._register_claude()
        self.assertFalse(migrate.claude_code_plugin_installed(self.home))
        self.assertEqual(migrate.apply(self.home), [])
        self.assertIn("memorysafe", json.loads(self.claude.read_text())["mcpServers"])

    def test_a_plugin_disabled_in_claude_code_settings_counts_as_not_installed(self) -> None:
        self._register_claude()
        self._install_claude_plugin(enabled=False)
        self.assertFalse(migrate.claude_code_plugin_installed(self.home))
        self.assertEqual(migrate.apply(self.home), [])
        self.assertIn("memorysafe", json.loads(self.claude.read_text())["mcpServers"])

    def test_a_plugin_enabled_in_claude_code_settings_counts_as_installed(self) -> None:
        self._install_claude_plugin(enabled=True)
        self.assertTrue(migrate.claude_code_plugin_installed(self.home))

    def test_a_codex_plugin_disabled_in_its_config_counts_as_not_installed(self) -> None:
        self.codex.write_text(CODEX_BEFORE + CODEX_PLUGIN.replace("enabled = true", "enabled = false"))
        self.assertFalse(migrate.codex_plugin_installed(self.home))
        self.assertEqual(migrate.apply(self.home), [])

    def test_an_unparseable_codex_config_means_no_codex_plugin(self) -> None:
        self.codex.write_text("[plugins.\"memorysafe@memorysafe\"]\nenabled = \n")
        self.assertFalse(migrate.codex_plugin_installed(self.home))

    def test_nothing_registered_means_nothing_written(self) -> None:
        self.codex.write_text("[mcp_servers.other]\ncommand = \"other\"\n")
        self.assertEqual(migrate.apply(self.home), [])
        self.assertFalse(list(self.home.rglob("*.memorysafe-backup")))

    def test_an_unreadable_claude_config_is_left_alone(self) -> None:
        self.claude.write_text("{not json")
        self.assertFalse(migrate.remove_claude_registration(self.claude))
        self.assertEqual(self.claude.read_text(), "{not json")

    def test_apply_removes_a_memorysafe_table_holding_a_multiline_array(self) -> None:
        self.codex.write_text(CODEX_MULTILINE_ARRAY + CODEX_PLUGIN)
        self.assertEqual(migrate.apply(self.home), [str(self.codex)])
        remaining = tomllib.loads(self.codex.read_text())
        self.assertNotIn("memorysafe", remaining["mcp_servers"])
        self.assertEqual(remaining["mcp_servers"]["other"], {"command": "other"})
        self.assertEqual(remaining["profiles"]["work"], {"model": "o3"})

    def test_apply_removes_a_memorysafe_table_holding_a_multiline_string(self) -> None:
        self.codex.write_text(CODEX_MULTILINE_STRING + CODEX_PLUGIN)
        self.assertEqual(migrate.apply(self.home), [str(self.codex)])
        remaining = tomllib.loads(self.codex.read_text())
        self.assertNotIn("memorysafe", remaining["mcp_servers"])
        self.assertEqual(remaining["mcp_servers"]["other"], {"command": "other"})
        self.assertEqual(remaining["profiles"]["work"], {"model": "o3"})

    def test_a_non_utf8_codex_config_is_left_alone(self) -> None:
        before = b"\xff\xfe[mcp_servers.memorysafe]\n"
        self.codex.write_bytes(before)
        self.assertFalse(migrate.codex_registration_present(self.codex))
        self.assertFalse(migrate.remove_codex_registration(self.codex))
        self.assertEqual(self.codex.read_bytes(), before)
        self.assertEqual(migrate.apply(self.home), [])

    def test_the_old_runtime_and_launch_agents_are_reported_but_kept(self) -> None:
        old = self.root / "claude-runtime" / "bin"
        old.mkdir(parents=True)
        (old / "python").write_bytes(b"x" * 2048)
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "ca.memorysafe.beta.setup.plist").write_text("plist")
        found = json.loads(self._cli("--apply", "--json"))["found"]
        self.assertEqual(found["old_runtime"]["bytes"], 2048)
        self.assertEqual(found["launch_agents"], ["ca.memorysafe.beta.setup.plist"])
        self.assertTrue((old / "python").is_file())


class ProjectRegistrationTests(MigrateTests):
    """Claude Code registers MCP servers per project too, and migrate only read the user one.

    A project entry survived every migrate, kept listing MemorySafe's tools a second time
    in that project, and is exactly the duplicate this command exists to remove.
    """

    def _project(self, name: str = "work") -> Path:
        project = Path(self.temporary.name) / name
        project.mkdir()
        return project

    def _register_project(self, project: Path, in_file: bool = False) -> None:
        config = json.loads(self.claude.read_text()) if self.claude.is_file() else {"theme": "dark"}
        config.setdefault("projects", {})[str(project)] = {
            "mcpServers": {"memorysafe": {"command": "y"}, "other": {"command": "x"}}
        }
        self.claude.write_text(json.dumps(config, indent=2))
        if in_file:
            (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"memorysafe": {"command": "y"}, "keep": {}}}))

    def test_a_dry_run_names_the_projects_and_changes_nothing(self) -> None:
        project = self._project()
        self._install_claude_plugin()
        self._register_project(project, in_file=True)
        before = self.claude.read_bytes()
        report = json.loads(self._cli("--json"))
        self.assertEqual(report["found"]["claude_code"]["projects"], [str(project)])
        self.assertEqual(report["found"]["claude_code"]["project_files"], [str(project / ".mcp.json")])
        self.assertEqual(self.claude.read_bytes(), before)

    def test_apply_removes_project_entries_and_keeps_the_users_other_servers(self) -> None:
        project = self._project()
        self._install_claude_plugin()
        self._register_project(project, in_file=True)
        self._cli("--apply")
        config = json.loads(self.claude.read_text())
        self.assertEqual(config["projects"][str(project)]["mcpServers"], {"other": {"command": "x"}})
        self.assertEqual(json.loads((project / ".mcp.json").read_text())["mcpServers"], {"keep": {}})
        self.assertTrue(self.claude.with_name(".claude.json.memorysafe-backup").is_file())

    def test_without_the_plugin_a_project_entry_is_that_projects_only_way_in(self) -> None:
        project = self._project()
        self._register_project(project, in_file=True)
        self._cli("--apply")
        config = json.loads(self.claude.read_text())
        self.assertIn("memorysafe", config["projects"][str(project)]["mcpServers"])
        self.assertIn("memorysafe", json.loads((project / ".mcp.json").read_text())["mcpServers"])

    def test_a_project_folder_that_is_gone_is_not_an_error(self) -> None:
        project = self._project()
        self._install_claude_plugin()
        self._register_project(project, in_file=True)
        shutil.rmtree(project)
        report = json.loads(self._cli("--apply", "--json"))
        self.assertEqual(report["found"]["claude_code"]["project_files"], [])


if __name__ == "__main__":
    unittest.main()
