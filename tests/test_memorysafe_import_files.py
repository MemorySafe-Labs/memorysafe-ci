"""memorysafe-import's reader: Claude Code memory files in, a MemorySafe export out.

The tool is stdlib-only and ships separately (uvx), so it is not installed with the package;
the tests put its source on sys.path the way a checkout would.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "memorysafe-import" / "src"))

from memorysafe_import import capture_policy as vendored_policy  # noqa: E402
from memorysafe_import import memories as reader  # noqa: E402


def _file(text: str, kind: str | None = "user", nested: bool = True, extra: str = "") -> str:
    if kind is None:
        return text
    if nested:
        meta = f"metadata: \n  node_type: memory\n  type: {kind}\n{extra}"
    else:
        meta = f"type: {kind}\n"
    return f"---\nname: n\ndescription: \"a description\"\n{meta}---\n\n{text}\n"


class ParseTests(unittest.TestCase):
    def test_frontmatter_type_maps_to_category(self):
        cases = [
            (_file("b", "user"), "personal"),
            (_file("b", "feedback", nested=False), "preference"),
            (_file("b", "project"), "project"),
            (_file("b", "reference"), "other"),
            (_file("b", "unknown"), "other"),
            ("just text, no frontmatter", "other"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected, text=text[:30]):
                self.assertEqual(reader.parse_memory(text, 0.0)[1], expected)

    def test_body_wins_then_description(self):
        content = reader.parse_memory(_file("The body."), 0.0)[0]
        self.assertEqual(content, "The body.")
        empty = "---\nname: n\ndescription: \"Only a description\"\n---\n\n"
        self.assertEqual(reader.parse_memory(empty, 0.0)[0], "Only a description")

    def test_links_become_plain_names(self):
        content = reader.parse_memory(_file("see [[nix-managed-global-git-config]]."), 0.0)[0]
        self.assertEqual(content, "see nix-managed-global-git-config.")

    def test_long_body_is_cut_at_a_paragraph(self):
        body = "\n\n".join(["p" * 99 for _ in range(120)])
        self.assertGreater(len(body), 11_000)
        content, _, _, shortened = reader.parse_memory(_file(body), 0.0)
        self.assertLessEqual(len(content), 10_000)
        self.assertTrue(shortened)
        self.assertTrue(content.endswith("p" * 99))
        self.assertEqual(len(content.split("\n\n")[-1]), 99)

    def test_modified_wins_over_mtime(self):
        text = _file("b", extra="  modified: 2026-09-07T03:35:57.285Z\n")
        self.assertEqual(reader.parse_memory(text, 0.0)[2], "2026-09-07T03:35:57+00:00")
        self.assertEqual(reader.parse_memory(_file("b"), 86400.0)[2], "1970-01-02T00:00:00+00:00")
        bad = _file("b", extra="  modified: not a date\n")
        self.assertEqual(reader.parse_memory(bad, 86400.0)[2], "1970-01-02T00:00:00+00:00")

    def test_broken_frontmatter_is_all_body(self):
        text = "---\nname: n\ntype: user\nno closing line"
        content, category, _, _ = reader.parse_memory(text, 0.0)
        self.assertEqual(content, text.strip())
        self.assertEqual(category, "other")


class FindTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()

    def folder(self, cwd: str | None, name: str | None = None) -> Path:
        folder = self.root / (name or reader.folder_name_for(Path(cwd)))
        (folder / "memory").mkdir(parents=True)
        if cwd is not None:
            line = json.dumps({"type": "user", "cwd": cwd}, separators=(",", ":"))
            (folder / "0b1f.jsonl").write_text(line + "\n", encoding="utf-8")
        return folder

    def test_memory_index_and_large_files_are_skipped(self):
        folder = self.folder("/home/a/work/memorysafe")
        (folder / "memory" / "MEMORY.md").write_text("- [x](x.md)\n", encoding="utf-8")
        (folder / "memory" / "big.md").write_text("x" * (70 * 1024), encoding="utf-8")
        (folder / "memory" / "ok.md").write_text(_file("fine", "project"), encoding="utf-8")
        found = reader.find_memories(self.root)
        self.assertEqual([m.label for m in found.memories], ["memorysafe/ok.md"])
        self.assertEqual(len(found.unreadable), 1)
        self.assertIn("big.md", found.unreadable[0])

    def test_project_from_session_cwd(self):
        folder = self.folder(None, "-home-a-work-memorysafe")
        (folder / "s.jsonl").write_text(
            '{"a":1,"cwd":"/home/a/work/memorysafe"}\n', encoding="utf-8")
        self.assertEqual(reader.project_name(folder), "memorysafe")

    def test_worktree_cwd_collapses_to_its_repo(self):
        folder = self.folder("/home/a/work/memorysafe/.claude/worktrees/bridge-x")
        self.assertEqual(reader.project_name(folder), "memorysafe")

    def test_no_session_falls_back_to_the_folder_name(self):
        folder = self.folder(None, "-home-a-notes")
        self.assertEqual(reader.project_name(folder), "home-a-notes")

    def test_personal_and_preference_have_no_project(self):
        folder = self.folder("/home/a/work/memorysafe")
        for name, kind in (("u.md", "user"), ("f.md", "feedback"), ("p.md", "project")):
            (folder / "memory" / name).write_text(_file(f"text {name}", kind), encoding="utf-8")
        found = {m.label: m for m in reader.find_memories(self.root).memories}
        self.assertEqual(found["You/u.md"].project, None)
        self.assertEqual(found["You/f.md"].project, None)
        self.assertEqual(found["memorysafe/p.md"].project, "memorysafe")

    def test_sensitive_memories_are_held_back(self):
        folder = self.folder("/home/a/work/memorysafe")
        (folder / "memory" / "mail.md").write_text(_file("write to alex@example.com", "project"), encoding="utf-8")
        (folder / "memory" / "long.md").write_text(_file("ordinary words. " * 125, "project"), encoding="utf-8")
        found = reader.find_memories(self.root)
        shape = vendored_policy.classify("write to alex@example.com")
        self.assertEqual(found.held_back, [("memorysafe/mail.md", vendored_policy.reason_for(shape))])
        self.assertEqual([m.label for m in found.memories], ["memorysafe/long.md"])
        self.assertGreaterEqual(len(found.memories[0].content), 1999)

    def test_only_limits_to_one_folder(self):
        one = self.folder("/home/a/work/memorysafe")
        two = self.folder("/home/a/work/other")
        (one / "memory" / "a.md").write_text(_file("one", "project"), encoding="utf-8")
        (two / "memory" / "b.md").write_text(_file("two", "project"), encoding="utf-8")
        both = reader.find_memories(self.root)
        self.assertEqual(both.folders, 2)
        only = reader.find_memories(self.root, only=reader.folder_name_for(Path("/home/a/work/memorysafe")))
        self.assertEqual([m.label for m in only.memories], ["memorysafe/a.md"])
        self.assertEqual(only.folders, 1)

    def test_unreadable_and_broken_files_do_not_stop_the_run(self):
        folder = self.folder("/home/a/work/memorysafe")
        (folder / "memory" / "latin.md").write_bytes("caf\xe9 \xff".encode("latin-1"))
        (folder / "memory" / "broken.md").write_text("---\nname: n\ntype: user\nopen", encoding="utf-8")
        found = reader.find_memories(self.root)
        self.assertEqual([m.label for m in found.memories], ["memorysafe/broken.md"])
        self.assertEqual(found.memories[0].category, "other")
        self.assertIn("open", found.memories[0].content)
        self.assertEqual(len(found.unreadable), 1)
        self.assertIn("latin.md", found.unreadable[0])

    def test_export_shape_reads_back(self):
        from memorysafe_chatgpt.transfer import parse_export

        mems = [
            reader.Memory("likes tea", "personal", "2026-09-07T03:35:57+00:00", None, "You/a.md", False),
            reader.Memory("ships friday", "project", "2026-09-08T00:00:00+00:00", "memorysafe", "memorysafe/b.md", False),
        ]
        data = reader.to_export(mems)
        self.assertEqual(len(parse_export(data)), 2)
        first, second = data["memories"]
        self.assertNotIn("project", first)
        self.assertEqual(second["project"], "memorysafe")
        self.assertEqual(first["source"], reader.SOURCE)
        self.assertIs(first["protected"], False)

    def test_claude_root_honours_config_dir(self):
        self.assertEqual(reader.claude_root({"CLAUDE_CONFIG_DIR": "/x/y"}), Path("/x/y/projects"))
        self.assertEqual(reader.claude_root({"HOME": "/h"}).name, "projects")


class VendoredPolicyTests(unittest.TestCase):
    def test_capture_policy_copy_is_identical(self):
        original = ROOT / "src" / "memorysafe_chatgpt" / "capture_policy.py"
        self.assertEqual(Path(vendored_policy.__file__).read_bytes(), original.read_bytes())


if __name__ == "__main__":
    unittest.main()
