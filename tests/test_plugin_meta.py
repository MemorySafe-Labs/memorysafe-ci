"""The runtime pins, and the committed files the POSIX launcher reads them from.

The launcher cannot hash a file portably or parse TOML, so the runtime key and the uv
checksums live in plain files beside it. These tests are what stop those files, the
lock and the pins from drifting apart.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
import unittest

from scripts import plugin_meta


class PluginMetaTests(unittest.TestCase):
    def test_committed_runtime_env_matches_the_lock(self) -> None:
        self.assertEqual(
            plugin_meta.RUNTIME_ENV.read_text(encoding="utf-8"),
            plugin_meta.runtime_env_text(plugin_meta.LOCK.read_bytes()),
        )

    def test_committed_uv_checksums_match_the_pins(self) -> None:
        self.assertEqual(
            plugin_meta.UV_CHECKSUMS_FILE.read_text(encoding="utf-8"),
            plugin_meta.uv_checksums_text(),
        )

    def test_runtime_key_follows_the_lock_and_the_python(self) -> None:
        key = plugin_meta.runtime_key(b"a==1\n", "3.12")
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertNotEqual(key, plugin_meta.runtime_key(b"a==2\n", "3.12"))
        self.assertNotEqual(key, plugin_meta.runtime_key(b"a==1\n", "3.13"))

    def test_every_direct_dependency_is_pinned_with_hashes(self) -> None:
        lock = plugin_meta.LOCK.read_text(encoding="utf-8")
        with (plugin_meta.ROOT / "pyproject.toml").open("rb") as handle:
            dependencies = tomllib.load(handle)["project"]["dependencies"]
        for dependency in dependencies:
            self.assertRegex(lock, rf"(?m)^{re.escape(dependency)} \\$", dependency)
        self.assertIn("--hash=sha256:", lock)

    def test_every_target_has_a_checksum_and_the_right_archive_suffix(self) -> None:
        """Every uv target ships as a nested .tar.gz, except Windows, which ships a flat
        .zip. This test used to hardcode the four POSIX target triples as a literal and
        assert plugin_meta.UV_CHECKSUMS equalled it. Adding the Windows pair meant
        hand-copying that literal to match UV_CHECKSUMS again, at which point it was only
        asserting "the constant equals itself" - it could never fail from a real mistake.
        This checks
        the properties that actually matter instead: every digest is well-formed sha256
        hex, and the generated uv-checksums text names each target's archive with the
        suffix its own platform ships, with no target missing or duplicated.
        """
        checksums = plugin_meta.UV_CHECKSUMS
        self.assertTrue(checksums)
        for target, digest in checksums.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", target)

        rows = [line.split() for line in plugin_meta.uv_checksums_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), len(checksums))

        seen = set()
        for digest, name in rows:
            self.assertRegex(digest, r"^[0-9a-f]{64}$", name)
            self.assertTrue(name.startswith("uv-") and name.endswith((".zip", ".tar.gz")), name)
            suffix = ".zip" if name.endswith(".zip") else ".tar.gz"
            target = name[len("uv-") : -len(suffix)]
            self.assertEqual(target.endswith("-pc-windows-msvc"), suffix == ".zip", name)
            self.assertIn(target, checksums, target)
            self.assertEqual(digest, checksums[target], target)
            seen.add(target)
        self.assertEqual(seen, set(checksums))

    def test_check_mode_accepts_the_committed_files(self) -> None:
        self.assertEqual(plugin_meta.main([]), 0)


class GitAttributesTests(unittest.TestCase):
    """A Windows checkout with the default core.autocrlf=true converts a committed
    LF text file to CRLF, and runtime_key() hashes LOCK.read_bytes() -- raw bytes,
    no newline normalization -- so the hash stopped matching the committed
    plugin/scripts/runtime.env and CI reported the runtime files as stale. Asks the
    real git binary rather than re-parsing .gitattributes, so this tests the actual
    mechanism a checkout uses.
    """

    def test_the_lock_file_is_forced_to_lf_on_checkout(self) -> None:
        result = subprocess.run(
            ["git", "check-attr", "eol", "--", str(plugin_meta.LOCK)],
            cwd=plugin_meta.ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("eol: lf", result.stdout)


if __name__ == "__main__":
    unittest.main()
