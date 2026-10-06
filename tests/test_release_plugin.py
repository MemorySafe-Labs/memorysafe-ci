"""scripts/release_plugin.py puts a build into a clone of the public repository.

Without --push it only commits and tags, so a release can be rehearsed. With --push,
which only the tag-triggered release workflow passes, it publishes: main, the tag and
the release-assets branch the public repository turns into a GitHub Release.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import plugin_meta, release_plugin


ROOT = Path(__file__).resolve().parents[1]
MCPB_BYTES = b"PK\x03\x04 not really a zip"


def _git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments], check=True, capture_output=True, text=True
    ).stdout


def _identify(checkout: Path) -> None:
    _git(checkout, "config", "user.email", "release@example.invalid")
    _git(checkout, "config", "user.name", "Release Test")


class GitHelperTests(unittest.TestCase):
    """subprocess's text=True (a.k.a. universal_newlines) mode translates "\\n" to
    os.linesep on WRITES to a child's stdin, not only on reads of its stdout. On
    Windows that turns every "\\n" in the `stdin` argument into "\\r\\n" before git
    ever sees it. _asset_commit() pipes a git mktree spec and a hash-object --stdin
    payload through _git(), so the CR would land inside the committed filename --
    CI showed "notes.md\\r" in the release-assets tree.
    """

    def test_never_asks_subprocess_for_text_mode(self) -> None:
        with patch.object(release_plugin.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"deadbeef\n")
            result = release_plugin._git(Path("/tmp/does-not-matter"), "hash-object", "-w", "--stdin", stdin="notes.md\n")
        _, kwargs = run.call_args
        self.assertNotIn("text", kwargs)
        self.assertNotIn("universal_newlines", kwargs)
        self.assertEqual(kwargs["input"], b"notes.md\n")
        self.assertEqual(result, "deadbeef\n")

    def test_no_stdin_passes_none_rather_than_an_empty_string(self) -> None:
        with patch.object(release_plugin.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"")
            release_plugin._git(Path("/tmp/does-not-matter"), "status", "--porcelain")
        _, kwargs = run.call_args
        self.assertIsNone(kwargs["input"])


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.checkout = base / "memorysafe-plugin"
        self.checkout.mkdir()
        _git(self.checkout, "init", "--quiet", "--initial-branch", "main")
        _identify(self.checkout)
        (self.checkout / "stale.txt").write_text("from the previous release\n")
        _git(self.checkout, "add", "stale.txt")
        _git(self.checkout, "commit", "--quiet", "--message", "Previous")
        self.output = base / "dist"
        self.version = plugin_meta.project_version()
        self.tag = f"v{self.version}"
        self.asset_branch = f"{release_plugin.ASSET_BRANCH_PREFIX}{self.tag}"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _fake_build(root: Path, output_dir: Path) -> tuple[Path, Path]:
        tree = output_dir / "memorysafe-plugin"
        shutil.rmtree(tree, ignore_errors=True)
        (tree / ".claude-plugin").mkdir(parents=True)
        (tree / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "memorysafe"}))
        start = tree / "plugins" / "memorysafe" / "scripts" / "start"
        start.parent.mkdir(parents=True)
        start.write_text("#!/bin/sh\n")
        start.chmod(0o755)
        mcpb = output_dir / "memorysafe-claude-desktop.mcpb"
        mcpb.write_bytes(MCPB_BYTES)
        return tree, mcpb

    def _release(self, **options) -> str:
        with patch.object(release_plugin.build_plugin, "build_plugin", side_effect=self._fake_build):
            return release_plugin.release(ROOT, self.checkout, self.output, **options)

    def _bare_remote(self, *, seeded: bool) -> Path:
        remote = Path(self.temporary.name) / "remote.git"
        _git(Path(self.temporary.name), "init", "--quiet", "--bare", str(remote))
        _git(self.checkout, "remote", "add", "origin", str(remote))
        if seeded:
            _git(self.checkout, "push", "--quiet", "origin", "main")
        else:
            # A clone of an empty repository: nothing committed yet.
            _git(self.checkout, "update-ref", "-d", "HEAD")
            _git(self.checkout, "rm", "--quiet", "--cached", "stale.txt")
            (self.checkout / "stale.txt").unlink()
        return remote

    def test_commits_and_tags_the_built_tree_without_pushing(self) -> None:
        self.assertEqual(self._release(), self.tag)
        self.assertEqual(
            set(_git(self.checkout, "ls-files").split()),
            {".claude-plugin/marketplace.json", "plugins/memorysafe/scripts/start"},
        )
        self.assertEqual(_git(self.checkout, "log", "-1", "--format=%s").strip(), f"MemorySafe {self.version}")
        self.assertEqual(_git(self.checkout, "tag", "--list").split(), [self.tag])
        self.assertEqual(_git(self.checkout, "cat-file", "-t", self.tag).strip(), "tag")
        self.assertEqual(_git(self.checkout, "remote").strip(), "")

    @unittest.skipIf(os.name == "nt", "asserts git ls-files -s reports 100755 for scripts/start; Windows "
                                       "git defaults core.filemode=false, so the index always records "
                                       "100644 for a freshly `git add`ed file regardless of what chmod "
                                       "set beforehand, and there is no per-repo override this test could "
                                       "make true on a real Windows checkout -- "
                                       "test_commits_and_tags_the_built_tree_without_pushing above still "
                                       "runs on Windows and covers everything else this release commits")
    def test_the_committed_start_script_keeps_the_posix_exec_mode(self) -> None:
        self._release()
        self.assertIn("100755", _git(self.checkout, "ls-files", "-s", "plugins/memorysafe/scripts/start"))

    def test_the_asset_branch_holds_only_the_extension_and_the_notes(self) -> None:
        """A deploy key can push but cannot create a GitHub Release, so the extension
        travels to the public repository on a branch of its own, outside main's history,
        where Claude Code and Codex would otherwise download it with every update."""
        self._release(notes="Plugin install for Claude Code and Codex.\n")
        files = _git(self.checkout, "ls-tree", "--name-only", self.asset_branch).split()
        self.assertEqual(sorted(files), [release_plugin.build_plugin.MCPB_NAME, release_plugin.NOTES_NAME])
        extension = subprocess.run(
            ["git", "-C", str(self.checkout), "show", f"{self.asset_branch}:{release_plugin.build_plugin.MCPB_NAME}"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(extension, MCPB_BYTES)
        notes = _git(self.checkout, "show", f"{self.asset_branch}:{release_plugin.NOTES_NAME}")
        self.assertEqual(notes, "Plugin install for Claude Code and Codex.\n")
        self.assertEqual(_git(self.checkout, "rev-list", "--count", self.asset_branch).strip(), "1")

    def test_notes_default_to_the_version(self) -> None:
        self._release(notes="  \n")
        notes = _git(self.checkout, "show", f"{self.asset_branch}:{release_plugin.NOTES_NAME}")
        self.assertEqual(notes, f"MemorySafe {self.version}\n")

    def test_refuses_a_tag_that_is_not_this_version(self) -> None:
        """The workflow passes the pushed tag. A v0.4.1 tag on a tree that still says
        0.4.0 would publish manifests the hosts see as unchanged, so nobody updates."""
        with self.assertRaises(SystemExit):
            self._release(tag="v0.0.0-not-this")
        self.assertEqual(_git(self.checkout, "tag", "--list").strip(), "")

    def test_refuses_a_clone_with_uncommitted_changes(self) -> None:
        (self.checkout / "notes.txt").write_text("unsaved\n")
        with self.assertRaises(SystemExit):
            self._release()

    def test_refuses_a_version_that_was_already_released(self) -> None:
        _git(self.checkout, "tag", self.tag)
        with self.assertRaises(SystemExit):
            self._release()

    def test_publish_pushes_main_the_tag_and_the_asset_branch(self) -> None:
        remote = self._bare_remote(seeded=True)
        tag = self._release()
        release_plugin.publish(self.checkout, tag)
        head = _git(self.checkout, "rev-parse", "HEAD").strip()
        self.assertEqual(_git(remote, "rev-parse", "refs/heads/main").strip(), head)
        self.assertEqual(_git(remote, "rev-parse", f"refs/tags/{tag}^{{commit}}").strip(), head)
        self.assertEqual(
            _git(remote, "rev-parse", f"refs/heads/{self.asset_branch}").strip(),
            _git(self.checkout, "rev-parse", self.asset_branch).strip(),
        )

    def test_the_first_release_into_an_empty_repository_publishes_main(self) -> None:
        """GitHub makes the first branch pushed into an empty repository its default,
        and the asset branch must never be the branch the hosts install from."""
        remote = self._bare_remote(seeded=False)
        tag = self._release()
        with patch.object(release_plugin, "_git", wraps=release_plugin._git) as git:
            release_plugin.publish(self.checkout, tag)
        pushes = [call.args[1:] for call in git.call_args_list if call.args[1] == "push"]
        self.assertEqual(pushes[0], ("push", "--quiet", "origin", "HEAD:refs/heads/main"))
        self.assertEqual(_git(remote, "rev-list", "--count", "refs/heads/main").strip(), "1")
        self.assertIn(tag, _git(remote, "tag", "--list"))

    def test_a_rerun_after_main_was_published_tags_it_without_a_new_commit(self) -> None:
        """If a publish pushed main and then failed, the next run starts from a clone
        whose main already holds this build. It tags that commit instead of failing on
        an empty commit."""
        self._bare_remote(seeded=True)
        self._release()
        release_plugin.publish(self.checkout, self.tag)
        published = _git(self.checkout, "rev-parse", "HEAD").strip()
        _git(self.checkout, "push", "--quiet", "origin", f":refs/tags/{self.tag}")
        _git(self.checkout, "tag", "--delete", self.tag)
        _git(self.checkout, "branch", "--quiet", "--delete", "--force", self.asset_branch)

        self.assertEqual(self._release(), self.tag)
        self.assertEqual(_git(self.checkout, "rev-parse", "HEAD").strip(), published)
        self.assertEqual(_git(self.checkout, "rev-parse", f"{self.tag}^{{commit}}").strip(), published)


class ReleaseWorkflowTests(unittest.TestCase):
    """Pushing a v* tag on main is the one way to publish. The tag's candidate is checked
    on the public repository, which reports public-ci/release on the tagged commit, and
    that status is what starts the publish. Everything the hosts install arrives within
    minutes of it, so these keep the gate in place."""

    WORKFLOWS = ROOT / ".github" / "workflows"

    def _read(self, name: str) -> str:
        return (self.WORKFLOWS / name).read_text(encoding="utf-8")

    def test_only_the_release_workflow_publishes(self) -> None:
        for path in self.WORKFLOWS.glob("*.yml"):
            if "--push" in path.read_text(encoding="utf-8") or "PLUGIN_DEPLOY_KEY" in path.read_text(encoding="utf-8"):
                self.assertEqual(path.name, "release.yml")

    def test_publishing_waits_for_the_public_run_on_a_release_candidate(self) -> None:
        """public-ci alone must not publish: every pull request sets it."""
        release = self._read("release.yml")
        self.assertIn("\non:\n  status:\n", release)
        self.assertNotIn("tags:", release)
        self.assertIn(
            "    if: >-\n"
            "      github.repository == 'MemorySafe-Labs/memorysafe'\n"
            "      && github.event.context == 'public-ci/release'\n"
            "      && github.event.state == 'success'\n",
            release,
        )
        self.assertIn("scripts/release_plugin.py", release)
        self.assertEqual(release.count("runs-on:"), 1)

    def test_the_release_lock_is_on_the_job_not_the_workflow(self) -> None:
        """Every commit status on the repository starts a run of this workflow, and the
        job then skips. A workflow-level concurrency group is taken before the job's if:
        is evaluated, and GitHub keeps one pending run per group, so a burst of statuses
        could cancel the pending run that carries the real public-ci/release success."""
        release = self._read("release.yml")
        header, jobs = release.split("\njobs:\n", 1)
        self.assertNotIn("concurrency:", header)
        self.assertIn("    concurrency:\n      group: release\n      cancel-in-progress: false\n", jobs)

    def test_it_publishes_the_commit_the_status_is_on(self) -> None:
        """A status run starts on the default branch whatever commit the status is on.
        Without this it would publish main's tip."""
        release = self._read("release.yml")
        self.assertIn("          ref: ${{ github.event.sha }}\n", release)

    def test_the_commit_must_carry_the_tag_that_names_its_version_on_main(self) -> None:
        """A status can be set by anyone holding the status App's key. A tag can only be pushed
        by a maintainer, so the tag is what authorises a release."""
        release = self._read("release.yml")
        self.assertIn('tag="v$version"', release)
        self.assertIn('"refs/tags/$tag^{commit}"', release)
        self.assertIn("git merge-base --is-ancestor HEAD origin/main", release)
        self.assertIn("--tag \"$TAG\"", release)


if __name__ == "__main__":
    unittest.main()
