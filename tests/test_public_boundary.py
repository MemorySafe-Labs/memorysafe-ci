"""Everything outside private/ is published: scripts/push_candidate.py pushes it to a
public repository on every pull request. So nothing public may depend on private/ --
it would break there -- or point a reader at a file that is not there."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# private/ as a repository path, and docs/ where the internal documents used to live.
# The lookbehind lets /private/tmp and /private/var through: those are macOS's own
# paths, in the installer scripts.
_NAMES_PRIVATE = re.compile(r"(?<![\w./-])(private/|docs/(superpowers|releasing|lifecycle-supersession))")
_IMPORTS_PRIVATE = re.compile(r"^\s*(from|import)\s+(memorysafe_hosted|memorysafe_orchestra)\b", re.MULTILINE)

# The files whose job is the boundary itself.
_MAY_NAME_IT = {
    "CLAUDE.md",
    "scripts/push_candidate.py",
    "tests/test_push_candidate.py",
    "tests/test_public_boundary.py",
    ".github/workflows/candidate.yml",
    ".github/workflows/publish-candidate.yml",
}


def _public_files() -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True
    )
    if listed.returncode != 0:
        raise unittest.SkipTest("not a git checkout, so there is no list of tracked files")
    return [name for name in listed.stdout.split("\0") if name and not name.startswith("private/")]


def _text(name: str) -> str:
    try:
        return (ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # Binary files, and a tracked symlink to a directory (a developer's .venv link).
        return ""


class PublicBoundaryTests(unittest.TestCase):
    def test_nothing_public_names_a_private_path(self) -> None:
        """Comments said "see docs/superpowers/specs/…" for the reason behind a rule.
        In the public tree that file does not exist, so the reason goes in the comment."""
        offenders = []
        for name in _public_files():
            if name in _MAY_NAME_IT:
                continue
            for number, line in enumerate(_text(name).splitlines(), 1):
                if _NAMES_PRIVATE.search(line):
                    offenders.append(f"{name}:{number}")
        self.assertEqual(offenders, [], f"these lines name a private path: {offenders}")

    def test_nothing_public_imports_private_code(self) -> None:
        """private/ may import the public package. The other direction would fail on
        the public repository, where private/ does not exist."""
        offenders = [
            name for name in _public_files()
            if name.endswith(".py") and name not in _MAY_NAME_IT and _IMPORTS_PRIVATE.search(_text(name))
        ]
        self.assertEqual(offenders, [])

    def test_the_shipped_package_holds_nothing_private(self) -> None:
        """orchestra*.py sat in src/memorysafe_chatgpt/ and was kept out of the plugin by
        an ignore pattern in the packager. One forgotten pattern published it; a folder
        cannot be forgotten."""
        shipped = [name for name in _public_files() if name.startswith("src/")]
        self.assertEqual([name for name in shipped if "orchestra" in name or "postgres" in name], [])


if __name__ == "__main__":
    unittest.main()
