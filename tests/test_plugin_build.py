"""The generated marketplace tree and Claude Desktop extension.

What testers install is this output, not the repository, so every promise about the
install is checked here: one version, exact launch lines, schemas Codex validates,
no machine paths, and the runtime inputs the launcher cannot start without.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from memorysafe_chatgpt.bootstrap_catalog import TOOLS
from scripts import plugin_meta
from scripts.build_plugin import ASSET_BRANCH_PREFIX, MCPB_NAME, NOTES_NAME, build_plugin


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "tests" / "fixtures" / "agent-plugins"


class PluginBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.tree, cls.mcpb = build_plugin(ROOT, Path(cls.temporary.name))
        cls.plugin = cls.tree / "plugins" / "memorysafe"
        cls.version = plugin_meta.project_version()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _shipped_names(self) -> list[str]:
        return [path.relative_to(self.tree).as_posix() for path in self.tree.rglob("*") if path.is_file()]

    def test_the_marketplace_lists_the_plugin_where_it_was_built(self) -> None:
        marketplace = self._json(self.tree / ".claude-plugin" / "marketplace.json")
        self.assertEqual(marketplace["name"], "memorysafe")
        self.assertTrue(marketplace["metadata"]["description"])
        (entry,) = marketplace["plugins"]
        self.assertEqual((entry["name"], entry["source"]), ("memorysafe", "./plugins/memorysafe"))
        self.assertTrue((self.tree / entry["source"]).is_dir())

    def test_every_manifest_carries_the_project_version(self) -> None:
        with zipfile.ZipFile(self.mcpb) as bundle:
            desktop = json.loads(bundle.read("manifest.json"))
        versions = {
            "marketplace": self._json(self.tree / ".claude-plugin" / "marketplace.json")["plugins"][0]["version"],
            "claude code": self._json(self.plugin / ".claude-plugin" / "plugin.json")["version"],
            "codex": self._json(self.plugin / "plugin.json")["version"],
            "claude desktop": desktop["version"],
        }
        self.assertEqual(versions, dict.fromkeys(versions, self.version))

    def test_claude_code_launches_through_an_extensionless_command(self) -> None:
        server = self._json(self.plugin / ".mcp.json")["mcpServers"]["memorysafe"]
        self.assertEqual(server, {"command": "${CLAUDE_PLUGIN_ROOT}/scripts/start", "args": []})

    def test_codex_launches_through_an_extensionless_command(self) -> None:
        server = self._json(self.plugin / "mcp.json")["mcpServers"]["memorysafe"]
        self.assertEqual(
            server,
            {"type": "stdio", "command": "./scripts/start", "args": [], "cwd": "${PLUGIN_ROOT}"},
        )

    def test_codex_files_validate_against_agent_plugins(self) -> None:
        for name in ("plugin", "mcp"):
            schema = self._json(SCHEMAS / f"{name}.schema.json")
            Draft202012Validator(schema).validate(self._json(self.plugin / f"{name}.json"))

    def test_codex_description_counts_the_tools_it_ships(self) -> None:
        interface = self._json(self.plugin / "plugin.json")["extensions"]["com.openai"]["interface"]
        self.assertEqual(len(TOOLS), 12)
        self.assertIn("twelve", interface["longDescription"])

    def test_the_runtime_inputs_ship_beside_the_launcher(self) -> None:
        for relative in (
            "scripts/start",
            "scripts/ensure_uv",
            "scripts/capture_hook",
            "scripts/capture_hook.py",
            "scripts/bootstrap_server.py",
            "scripts/degraded_server.py",
            "scripts/install_runtime.py",
            "scripts/runtime.env",
            "scripts/uv-checksums",
            "requirements.lock",
            "hooks/hooks.json",
            "skills/memorysafe/SKILL.md",
            "src/memorysafe_chatgpt/bootstrap_catalog.py",
            "src/memorysafe_chatgpt/provisioning.py",
            "src/memorysafe_chatgpt/progress_page.py",
            "assets/memorysafe-icon.png",
        ):
            self.assertTrue((self.plugin / relative).is_file(), relative)
        self.assertEqual(
            (self.plugin / "scripts" / "runtime.env").read_text(),
            plugin_meta.runtime_env_text((self.plugin / "requirements.lock").read_bytes()),
        )

    def test_the_windows_launchers_ship(self) -> None:
        """The marketplace tree used to strip *.cmd, which is why Windows had no
        install route at all. It must now carry them.

        plugin/mcp.json and plugin/.mcp.json name the extensionless scripts/start
        with no win32 override, because Windows resolves the extension and finds
        start.cmd -- and JSON has no comments, so nothing in those files can say
        so. This test is what catches a _PLUGIN_IGNORE regression dropping the
        pair from the marketplace tree. It is not the only check on those names:
        test_windows_launchers_ship_beside_their_posix_twins in
        test_claude_packaging.py checks all four in source, and
        test_the_desktop_extension_launches_the_same_script_and_lists_the_catalog
        below checks start and start.cmd inside the .mcpb, which
        build_claude_packages builds without consulting _PLUGIN_IGNORE at all.
        """
        scripts = self.plugin / "scripts"
        for name in ("start", "start.cmd", "ensure_uv", "ensure_uv.cmd"):
            self.assertTrue((scripts / name).is_file(), name)
        self.assertFalse([n for n in self._shipped_names() if n.endswith(".pyc")])

    @unittest.skipIf(os.name == "nt", "asserts a live 0o755 stat; NTFS has no POSIX exec bit for the "
                                       "build to preserve, so start/ensure_uv/capture_hook cannot carry "
                                       "0o755 through a Windows checkout the way they do on macOS and "
                                       "Linux -- the ship-check above still runs on Windows and is what "
                                       "matters there")
    def test_the_windows_launchers_keep_the_posix_modes(self) -> None:
        """start/ensure_uv/capture_hook keep 0o755 - both hosts copy the plugin into a
        versioned cache and preserve mode."""
        scripts = self.plugin / "scripts"
        for name in ("start", "ensure_uv", "capture_hook"):
            self.assertEqual((scripts / name).stat().st_mode & 0o777, 0o755, name)

    def test_nothing_else_ships(self) -> None:
        shipped = self._shipped_names()
        self.assertFalse([name for name in shipped if "__pycache__" in name])
        for absent in ("manifest.json", "mcpb_server.py", "INSTALL.md"):
            self.assertNotIn(f"plugins/memorysafe/{absent}", shipped)

    def test_no_generated_file_holds_a_machine_path(self) -> None:
        """The Codex manifest once launched /Users/someone/...; the build must not
        let a developer's machine into what testers install."""
        forbidden = ("/Users/", str(Path.home()), str(ROOT))
        for path in self.tree.rglob("*"):
            if not path.is_file() or path.suffix == ".png":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{path.relative_to(self.tree)} contains {needle}")

    def test_the_readme_gives_both_install_commands(self) -> None:
        readme = (self.tree / "README.md").read_text()
        for command in (
            "claude plugin marketplace add MemorySafe-Labs/memorysafe-plugin",
            "claude plugin install memorysafe@memorysafe",
            "codex plugin marketplace add MemorySafe-Labs/memorysafe-plugin",
            "codex plugin add memorysafe@memorysafe",
            f"releases/latest/download/{MCPB_NAME}",
            # A first-start download left unnamed makes "nothing leaves this computer" untrue.
            "tokenizer data",
        ):
            self.assertIn(command, readme)
        self.assertTrue((self.tree / "LICENSE").is_file())

    def test_the_desktop_extension_launches_the_same_script_and_lists_the_catalog(self) -> None:
        self.assertEqual(self.mcpb.name, MCPB_NAME)
        with zipfile.ZipFile(self.mcpb) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            names = set(bundle.namelist())
        self.assertEqual(manifest["server"]["entry_point"], "scripts/start")
        self.assertEqual(
            (manifest["server"]["mcp_config"]["command"], manifest["server"]["mcp_config"]["args"]),
            ("/bin/sh", ["${__dirname}/scripts/start"]),
        )
        self.assertEqual(
            manifest["tools"],
            [{"name": tool["name"], "description": tool["description"]} for tool in TOOLS],
        )
        for relative in (
            "scripts/start",
            "scripts/ensure_uv",
            "scripts/runtime.env",
            "scripts/uv-checksums",
            "requirements.lock",
            "scripts/start.cmd",
            "scripts/install_runtime.py",
        ):
            self.assertIn(relative, names)

    def test_uv_checksums_cover_every_shipped_target(self) -> None:
        """Windows ships a .zip while POSIX ships a .tar.gz.

        ensure_uv.cmd looks the archive up by its exact filename, so a missing or
        misnamed Windows row makes it exit 3 - "no checksum is recorded" - on a
        machine that is otherwise fine.
        """
        expected = {
            "uv-aarch64-apple-darwin.tar.gz",
            "uv-x86_64-apple-darwin.tar.gz",
            "uv-aarch64-unknown-linux-musl.tar.gz",
            "uv-x86_64-unknown-linux-musl.tar.gz",
            "uv-aarch64-pc-windows-msvc.zip",
            "uv-x86_64-pc-windows-msvc.zip",
        }
        text = (ROOT / "plugin" / "scripts" / "uv-checksums").read_text(encoding="utf-8")
        names = {line.split()[1] for line in text.splitlines() if line.strip()}
        self.assertEqual(names, expected)
        for line in text.splitlines():
            if line.strip():
                digest = line.split()[0]
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_public_repository_turns_a_tag_into_a_release(self) -> None:
        """The source repository's deploy key can push to the public repository but
        cannot create a release there, so the public tree carries the workflow that does,
        from the asset branch release_plugin.py pushes with the tag."""
        workflow = (self.tree / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn('    tags: ["v*"]\n', workflow)
        self.assertNotIn("pull_request", workflow)
        self.assertNotIn("branches:", workflow)
        self.assertIn("  contents: write\n", workflow)
        self.assertIn(f"ref: {ASSET_BRANCH_PREFIX}${{{{ github.ref_name }}}}\n", workflow)
        self.assertIn(f'--delete "{ASSET_BRANCH_PREFIX}$TAG"', workflow)
        self.assertIn(MCPB_NAME, workflow)
        self.assertIn(f"--notes-file {NOTES_NAME}", workflow)

    def test_public_readme_comes_from_an_authored_file(self) -> None:
        """The public README was a Python string literal inside build_plugin.py.

        A user-facing document there cannot be reviewed as a diff, which is how it
        shipped for three releases missing the Windows migrate path while the release
        notes announced Windows parity. It is an authored file now, and this test
        fails if it moves back into code.
        """
        root = Path(__file__).resolve().parents[1]
        template = root / "plugin" / "README-template.md"
        self.assertTrue(template.is_file(), "plugin/README-template.md is missing")
        source = (root / "scripts" / "build_plugin.py").read_text(encoding="utf-8")
        self.assertNotIn("_README = ", source)
        self.assertIn("{version}", template.read_text(encoding="utf-8"))

    def test_both_public_readmes_come_from_one_source(self) -> None:
        """Two READMEs gave install commands and nothing tied them together.

        The root README and plugins/memorysafe/README.md had already drifted: only
        one carried the First start and migrate sections, and neither carried the
        Windows migrate path. The same defect had to be fixed twice and was fixed
        in neither. They are now one generated document.
        """
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            tree, _mcpb = build_plugin(root, Path(temporary))
            outer = (tree / "README.md").read_text(encoding="utf-8")
            inner = (tree / "plugins" / "memorysafe" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(outer, inner)
        self.assertIn("%LOCALAPPDATA%\\MemorySafe\\bin\\memorysafe.cmd migrate", outer)

    def test_the_install_guide_image_ships_beside_the_readme_not_in_the_plugin(self) -> None:
        """The download is only the .mcpb, and nothing in it is written for a person.

        The instructions live on the release page and in the README, which a tester on
        19 Sep said nobody reads -- the image is the part that gets looked at. It has to be
        in the published tree for the README's link to resolve, and it must stay out of
        the plugin directory, which every host clones into every install.
        """
        self.assertTrue((self.tree / "install-guide.png").is_file())
        self.assertFalse((self.tree / "plugins" / "memorysafe" / "install-guide.png").exists())
        readme = (self.tree / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/MemorySafe-Labs/memorysafe-plugin/main/install-guide.png", readme
        )

    def test_install_documents_describe_the_same_routes(self) -> None:
        """Three documents gave install commands and none agreed with the others.

        plugin/README.md, the README literal inside build_plugin.py and INSTALL.md
        each listed install routes. All three omitted the Windows migrate path while
        the 0.4.1 notes announced Windows parity, because fixing it meant finding and
        editing three places. A route added to one document and not the other now
        fails here instead of shipping.
        """
        import re  # test_plugin_build.py does not import re at module level

        root = Path(__file__).resolve().parents[1]
        pattern = re.compile(r"^\s*((?:claude|codex) plugin [^\n]+)$", re.MULTILINE)

        def routes(text: str) -> set[str]:
            found = set()
            for line in pattern.findall(text):
                # Compare the verb, not the argument -- the argument is a constant
                # either way, but the route (marketplace add / install / plugin add)
                # is the part that must agree between the two documents.
                found.add(" ".join(line.split()[:3]))
            return found

        # plugin/README-template.md holds {repository}/{plugin}@{marketplace}/{mcpb}
        # placeholders -- test_plugin_readme_keeps_its_promises hit this already and
        # reads the rendered README rather than the raw template for the same reason.
        # self.tree/README.md is that render, already built once in setUpClass.
        readme = (self.tree / "README.md").read_text(encoding="utf-8")
        install = (root / "plugin" / "INSTALL.md").read_text(encoding="utf-8")

        self.assertEqual(routes(readme), routes(install))
        self.assertEqual(
            routes(readme),
            {
                "claude plugin marketplace",
                "claude plugin install",
                "codex plugin marketplace",
                "codex plugin add",
            },
        )
        for text, name in ((readme, "README.md"), (install, "INSTALL.md")):
            self.assertIn("memorysafe-claude-desktop.mcpb", text, name)
            self.assertIn("127.0.0.1:8765/dashboard", text, name)
            self.assertIn("estart", text, name)  # "Restart" or "restart"

        # Migrate paths, not just install routes. The Windows line was missing from both
        # documents in the release that announced Windows parity, and was then added to
        # only one of them -- the same defect twice, for the same reason. INSTALL.md
        # writes "migrate --apply" where the README writes "migrate", so match the shim
        # path up to the verb and let either suffix follow.
        for shim in (
            "~/.local/share/MemorySafe/bin/memorysafe migrate",
            "~/Library/Application\\ Support/MemorySafe/bin/memorysafe migrate",
            "%LOCALAPPDATA%\\MemorySafe\\bin\\memorysafe.cmd migrate",
        ):
            for text, name in ((readme, "README.md"), (install, "INSTALL.md")):
                self.assertIn(shim, text, f"{name} is missing the migrate line for {shim}")

    def test_the_install_documents_promise_the_progress_page(self) -> None:
        """Both documents named the dashboard as the proof an install worked while it
        refused connections for the whole first build. The claim changes with the code."""
        root = Path(__file__).resolve().parents[1]
        readme = (self.tree / "README.md").read_text(encoding="utf-8")
        install = (root / "plugin" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("shows that setup's progress", readme)
        self.assertNotIn("When that finishes,\n<http://127.0.0.1:8765/dashboard> opens", readme)
        self.assertIn("<http://127.0.0.1:8765/dashboard> shows each step", install)
        self.assertIn("it tells you which step it is on", install)

    def test_the_licence_ships_the_privacy_policy_it_incorporates(self) -> None:
        """LICENSE is the Beta Terms, and it names "the Privacy Policy" as forming
        part of the Terms at four separate points. That document was published
        nowhere in the public repository, so every user of the plugin agreed to
        terms incorporating a document they had no way to read.
        """
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            tree, _mcpb = build_plugin(root, Path(temporary))
            licence = (tree / "LICENSE").read_text(encoding="utf-8")
            self.assertIn("Privacy Policy", licence)
            policy = tree / "PRIVACY.md"
            self.assertTrue(policy.is_file(), "LICENSE incorporates a policy that is not published")
            self.assertIn("MemorySafe Beta Privacy Policy", policy.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
