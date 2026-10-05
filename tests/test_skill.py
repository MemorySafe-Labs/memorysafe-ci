"""The skill both plugins ship. It told the model to call memorysafe_dashboard, which
the server no longer has."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from memorysafe_chatgpt.bootstrap_catalog import TOOLS


SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "memorysafe" / "SKILL.md"


class SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_every_tool_it_names_exists(self) -> None:
        named = set(re.findall(r"\bmemorysafe_[a-z_]+", self.text))
        self.assertTrue(named)
        self.assertLessEqual(named, {tool["name"] for tool in TOOLS})

    def test_it_is_not_written_for_one_host(self) -> None:
        self.assertTrue(self.text.startswith("---\nname: memorysafe\n"))
        for host in ("Claude Code", "Codex", "Claude Desktop"):
            self.assertIn(host, self.text)

    def test_diagnosis_starts_with_the_tool_and_migration_needs_confirmation(self) -> None:
        self.assertIn("memorysafe_doctor", self.text)
        self.assertIn("memorysafe migrate --apply", self.text)
        self.assertIn("only after they confirm", self.text)

    def test_migrate_is_run_at_its_full_path(self) -> None:
        """The CLI lives in the data root and is never put on PATH, so a bare
        `memorysafe migrate` fails in every terminal the model opens."""
        self.assertIn("~/.local/share/MemorySafe/bin/memorysafe migrate", self.text)
        self.assertIn("Application Support/MemorySafe/bin/memorysafe\" migrate", self.text)
        self.assertNotRegex(self.text, r"(?<![/\"])`memorysafe migrate")


if __name__ == "__main__":
    unittest.main()
