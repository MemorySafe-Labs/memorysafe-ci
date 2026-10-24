"""scripts/push_candidate.py publishes source to a public repository, and publication
cannot be undone. These check what leaves the machine: the tree without private/, one
commit with no parent, nobody's name on it, and nothing at all when a guard refuses."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import push_candidate

INTERNAL_MESSAGE = "Internal message that must not be published"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": on Windows the default writes CRLF, and whether git then stores LF
    # depends on the runner's core.autocrlf.
    path.write_text(text, encoding="utf-8", newline="\n")


class _SourceRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "source"
        self.repo.mkdir()
        self.remote = self.tmp / "public.git"
        _git(self.tmp, "init", "--quiet", "--bare", str(self.remote))
        _git(self.repo, "init", "--quiet", "--initial-branch", "feature/x")
        _git(self.repo, "config", "user.email", "developer@example.com")
        _git(self.repo, "config", "user.name", "A Developer")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _write(self.repo / "README.md", "public\n")
        _write(self.repo / "src" / "app.py", "print('public')\n")
        _write(self.repo / "private" / "docs" / "plan.md", "secret plan\n")
        _write(self.repo / "private" / "src" / "hosted.py", "SECRET = 1\n")
        self._commit(INTERNAL_MESSAGE)

    def _commit(self, message: str) -> str:
        _git(self.repo, "add", "--all")
        _git(self.repo, "commit", "--quiet", "--message", message)
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def _paths(self, tree: str) -> list[str]:
        return _git(self.repo, "ls-tree", "-r", "--name-only", tree).split()


class PublicTreeTests(_SourceRepository):
    def test_the_tree_has_everything_except_private(self) -> None:
        tree = push_candidate.public_tree(self.repo)
        self.assertEqual(self._paths(tree), ["README.md", "src/app.py"])

    def test_building_it_touches_neither_the_index_nor_the_working_tree(self) -> None:
        """The tree is built in a temporary index. Using the real one would unstage
        whatever the developer had staged, and leave private/ staged for deletion."""
        _write(self.repo / "untracked.txt", "mine\n")
        before = _git(self.repo, "status", "--porcelain")
        push_candidate.public_tree(self.repo)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), before)
        self.assertTrue((self.repo / "private" / "docs" / "plan.md").exists())

    def test_an_untracked_file_cannot_reach_it(self) -> None:
        _write(self.repo / "notes.txt", "not committed\n")
        self.assertNotIn("notes.txt", self._paths(push_candidate.public_tree(self.repo)))

    def test_an_uncommitted_change_is_refused(self) -> None:
        """The result is attached to one private SHA, so the candidate has to be that
        commit and nothing else."""
        _write(self.repo / "README.md", "edited\n")
        with self.assertRaises(push_candidate.Refusal):
            push_candidate.source_commit(self.repo)

    def test_a_clean_tree_gives_the_head_commit(self) -> None:
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(push_candidate.source_commit(self.repo), head)


# Stands in for whatever is listed in PRIVATE_PATHS_OUTSIDE_PRIVATE at the time. The real
# tuple is empty, so the mechanism is proved by patching a pattern in, not by planting a
# file the real list happens to name.
_INTERIM_PRIVATE = ("tests/test_interim_private_*",)
_INTERIM_FILE = "tests/test_interim_private_schema.py"


def _interim_private():
    return patch.object(push_candidate, "PRIVATE_PATHS_OUTSIDE_PRIVATE", _INTERIM_PRIVATE)


class LeakGuardTests(_SourceRepository):
    def test_a_clean_tree_has_no_leaks(self) -> None:
        self.assertEqual(push_candidate.leaks(self.repo, push_candidate.public_tree(self.repo)), [])

    def test_the_interim_deny_list_is_empty(self) -> None:
        """The hosted edition moved into private/, so nothing needs listing outside it.
        If this fails, something private is sitting where the public mirror will see it
        and someone put it on the list on purpose: check that it is meant to be there."""
        self.assertEqual(push_candidate.PRIVATE_PATHS_OUTSIDE_PRIVATE, ())

    def test_private_code_outside_private_is_a_leak_when_listed(self) -> None:
        """Hosted code once sat in src/ and tests/ until it moved, and the tuple that
        named it is what kept it off the public mirror. The boundary test runs in CI,
        which is after publication, so the script checks first. A pattern listed in
        PRIVATE_PATHS_OUTSIDE_PRIVATE must still be caught, wherever it is."""
        _write(self.repo / _INTERIM_FILE, "x = 1\n")
        self._commit("interim")
        with _interim_private():
            self.assertEqual(
                push_candidate.leaks(self.repo, push_candidate.public_tree(self.repo)),
                [_INTERIM_FILE],
            )

    def test_a_tree_that_still_holds_private_is_a_leak(self) -> None:
        head_tree = _git(self.repo, "rev-parse", "HEAD^{tree}").strip()
        self.assertEqual(
            push_candidate.leaks(self.repo, head_tree),
            ["private/docs/plan.md", "private/src/hosted.py"],
        )


class CandidateCommitTests(_SourceRepository):
    def _candidate(self) -> tuple[str, str]:
        source = push_candidate.source_commit(self.repo)
        tree = push_candidate.public_tree(self.repo)
        return source, push_candidate.candidate_commit(self.repo, tree, "candidate/feature/x", source)

    def test_it_has_no_parent(self) -> None:
        """A parent would carry this repository's history, commit messages and the
        author addresses that were purged from it."""
        _, commit = self._candidate()
        self.assertEqual(_git(self.repo, "rev-list", "--count", commit).strip(), "1")
        self.assertNotIn(INTERNAL_MESSAGE, _git(self.repo, "log", "--format=%B", commit))

    def test_nobody_is_named_on_it(self) -> None:
        _, commit = self._candidate()
        people = _git(self.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", commit).strip()
        self.assertEqual(people, "MemorySafe CI <ci@memorysafe.invalid>|MemorySafe CI <ci@memorysafe.invalid>")

    def test_it_names_the_private_commit_it_stands_for(self) -> None:
        """candidate.yml reads this trailer to know which private commit to report on."""
        source, commit = self._candidate()
        message = _git(self.repo, "log", "-1", "--format=%B", commit)
        self.assertEqual(message.strip(), f"Candidate for candidate/feature/x\n\nSource-Commit: {source}")

    def test_the_ref_follows_the_branch_or_the_release_tag(self) -> None:
        self.assertEqual(push_candidate.candidate_ref(self.repo, None, None), "candidate/feature/x")
        self.assertEqual(push_candidate.candidate_ref(self.repo, "fix/y", None), "candidate/fix/y")
        self.assertEqual(push_candidate.candidate_ref(self.repo, None, "v0.4.12"), "release-candidate/v0.4.12")

    def test_a_detached_head_needs_a_name(self) -> None:
        """actions/checkout leaves HEAD detached, so the workflow passes --branch."""
        _git(self.repo, "checkout", "--quiet", "--detach")
        with self.assertRaises(push_candidate.Refusal):
            push_candidate.candidate_ref(self.repo, None, None)


class DryRunTests(_SourceRepository):
    def test_it_writes_the_public_tree_and_pushes_nothing(self) -> None:
        out = self.tmp / "audit"
        self.assertEqual(push_candidate.main(["--dry-run", str(out), "--remote", str(self.remote)], repo=self.repo), 0)
        self.assertEqual((out / "src" / "app.py").read_text(encoding="utf-8"), "print('public')\n")
        self.assertFalse((out / "private").exists())
        self.assertEqual(_git(self.remote, "for-each-ref").strip(), "")

    def test_a_leak_stops_it_before_anything_is_written(self) -> None:
        _write(self.repo / _INTERIM_FILE, "x = 1\n")
        self._commit("interim")
        out = self.tmp / "audit"
        with _interim_private():
            code = push_candidate.main(["--dry-run", str(out), "--remote", str(self.remote)], repo=self.repo)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


class _FakeGh:
    """Records every gh call. `gh run list` answers with one run; `gh run watch` exits
    with `watch_exit`."""

    def __init__(self, watch_exit: int = 0, runs: bool = True, list_output: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.watch_exit = watch_exit
        self.runs = runs
        self.list_output = list_output

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ("run", "list"):
            runs = [{"databaseId": 7, "url": "https://example.test/runs/7"}] if self.runs else []
            return subprocess.CompletedProcess(args, 0, self.list_output or json.dumps(runs), "")
        if args[:2] == ("run", "watch"):
            return subprocess.CompletedProcess(args, self.watch_exit, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def statuses(self) -> list[tuple[str, str]]:
        """(state, context) of each status set, in order."""
        found = []
        for call in self.calls:
            if call[0] == "api":
                fields = dict(field.split("=", 1) for field in call if "=" in field)
                found.append((fields["state"], fields["context"]))
        return found


class PushTests(_SourceRepository):
    def _run(self, *args: str, gh: _FakeGh | None = None) -> tuple[int, _FakeGh]:
        gh = gh or _FakeGh()
        with patch.object(push_candidate, "_gh", gh):
            code = push_candidate.main([*args, "--remote", str(self.remote)], repo=self.repo)
        return code, gh

    def test_the_public_repository_receives_one_commit_and_no_private_file(self) -> None:
        code, _ = self._run("--no-wait")
        self.assertEqual(code, 0)
        ref = "refs/heads/candidate/feature/x"
        self.assertEqual(_git(self.remote, "rev-list", "--count", ref).strip(), "1")
        self.assertEqual(_git(self.remote, "ls-tree", "-r", "--name-only", ref).split(), ["README.md", "src/app.py"])
        self.assertEqual(_git(self.remote, "rev-list", "--all", "--count").strip(), "1")

    def test_a_second_push_replaces_the_first(self) -> None:
        self._run("--no-wait")
        _write(self.repo / "README.md", "second\n")
        self._commit("second")
        code, _ = self._run("--no-wait")
        self.assertEqual(code, 0)
        self.assertEqual(_git(self.remote, "show", "refs/heads/candidate/feature/x:README.md"), "second\n")
        self.assertEqual(_git(self.remote, "rev-list", "--count", "refs/heads/candidate/feature/x").strip(), "1")

    def test_a_release_tag_gets_its_own_ref_and_context(self) -> None:
        """release.yml publishes on public-ci/release alone, so an ordinary pull request
        passing can never publish anything."""
        code, gh = self._run("--no-wait", "--release-tag", "v0.4.12")
        self.assertEqual(code, 0)
        self.assertIn("refs/heads/release-candidate/v0.4.12", _git(self.remote, "for-each-ref"))
        self.assertEqual(gh.statuses(), [("pending", "public-ci/release")])

    def test_a_branch_named_release_does_not_get_the_release_context(self) -> None:
        """A branch named release/0.4.5 existed in this repository, and public-ci/release
        is what lets release.yml publish. Branch candidates must never produce it."""
        code, gh = self._run("--no-wait", "--branch", "release/0.4.5")
        self.assertEqual(code, 0)
        self.assertIn("refs/heads/candidate/release/0.4.5", _git(self.remote, "for-each-ref"))
        self.assertEqual(gh.statuses(), [("pending", "public-ci")])

    def test_a_leak_pushes_nothing_and_sets_no_status(self) -> None:
        _write(self.repo / _INTERIM_FILE, "x = 1\n")
        self._commit("interim")
        with _interim_private():
            code, gh = self._run("--no-wait")
        self.assertEqual(code, 1)
        self.assertEqual(_git(self.remote, "for-each-ref").strip(), "")
        self.assertEqual(gh.calls, [])

    def test_a_failed_push_is_reported_and_sets_no_status(self) -> None:
        gh = _FakeGh()
        with patch.object(push_candidate, "_gh", gh):
            code = push_candidate.main(["--no-wait", "--remote", str(self.tmp / "missing.git")], repo=self.repo)
        self.assertEqual(code, 1)
        self.assertEqual(gh.calls, [])

    def test_no_wait_marks_the_private_commit_pending_and_stops(self) -> None:
        """In the workflow the public run sets the result. Pending is set here so the
        pull request shows a check from the first second."""
        source = _git(self.repo, "rev-parse", "HEAD").strip()
        code, gh = self._run("--no-wait")
        self.assertEqual(code, 0)
        self.assertEqual(gh.statuses(), [("pending", "public-ci")])
        self.assertEqual(gh.calls[0][:2], ("api", f"repos/MemorySafe-Labs/memorysafe/statuses/{source}"))

    def test_by_hand_it_waits_and_reports_success(self) -> None:
        code, gh = self._run(gh=_FakeGh(watch_exit=0))
        self.assertEqual(code, 0)
        self.assertEqual(gh.statuses(), [("pending", "public-ci"), ("success", "public-ci")])
        self.assertIn("target_url=https://example.test/runs/7", gh.calls[-1])

    def test_by_hand_it_waits_and_reports_failure(self) -> None:
        code, gh = self._run(gh=_FakeGh(watch_exit=1))
        self.assertEqual(code, 1)
        self.assertEqual(gh.statuses(), [("pending", "public-ci"), ("failure", "public-ci")])

    def test_a_run_that_never_appears_is_an_error_not_a_pass(self) -> None:
        with patch.object(push_candidate, "_WATCH", (2, 0.0)):
            code, gh = self._run(gh=_FakeGh(runs=False))
        self.assertEqual(code, 1)
        self.assertEqual(gh.statuses(), [("pending", "public-ci"), ("error", "public-ci")])

    def test_unparseable_run_list_output_is_no_run_yet_not_a_traceback(self) -> None:
        """gh printed something that was not JSON after the push had succeeded. The parse
        raised, and the private commit stayed on pending with nothing to say why."""
        with patch.object(push_candidate, "_WATCH", (2, 0.0)):
            code, gh = self._run(gh=_FakeGh(list_output="not json"))
        self.assertEqual(code, 1)
        self.assertEqual(gh.statuses(), [("pending", "public-ci"), ("error", "public-ci")])


if __name__ == "__main__":
    unittest.main()
