from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from memorysafe_chatgpt import dashboard
from memorysafe_chatgpt.dashboard import dashboard_html, panel_html


class DashboardCopyTests(unittest.TestCase):
    def test_no_served_page_calls_the_computer_a_mac(self) -> None:
        """Both pages are served on every platform and said "On this Mac".

        claude_launcher._start_dashboard() spawns setup_app unconditionally, and
        setup_app serves /dashboard and /panel behind a loopback check with no
        platform check, so a Windows plugin user opens both. They told that user
        their Windows machine was a Mac, in the release that announced Windows
        parity.

        Matched as a whole word, not as a substring: both pages' font stacks name
        BlinkMacSystemFont, a standard CSS token that must not be renamed -- it is
        how the page picks the system font on macOS. A bare "Mac" check trips on
        it and would have to be "fixed" by breaking the font stack.

        orchestra_html is deliberately absent: it has no Mac string and nothing
        serves it -- the retired internal chat room has no route.
        """
        for page, render in (("dashboard", dashboard_html), ("panel", panel_html)):
            with self.subTest(page=page):
                self.assertNotRegex(render(), r"\bMac\b", f"/{page} is served on Windows and Linux too")


class HostedDashboardTests(unittest.TestCase):
    def test_local_output_is_unchanged(self) -> None:
        # Golden file: origin/main's dashboard_html() at 7699593 (0.4.27), rendered from
        # main's own code, never from a branch. When main changes the local page on purpose,
        # regenerate it:  python -c "from memorysafe_chatgpt.dashboard import dashboard_html;
        # open('tests/fixtures/dashboard_v3_local.html','wb').write(dashboard_html().encode('utf-8'))"
        expected = (Path(__file__).parent / "fixtures" / "dashboard_v3_local.html").read_bytes().decode("utf-8")
        self.assertTrue(
            dashboard_html() == expected,
            "local dashboard differs from tests/fixtures/dashboard_v3_local.html; if main changed it on purpose, "
            "regenerate the fixture from main's dashboard_html() (command in this test's comment)",
        )

    def test_local_page_never_runs_the_hosted_wording(self) -> None:
        # The guard that does not need the fixture: main's ordinary edits to the local page
        # pass as long as the local path skips _hosted_wording and carries no hosted text.
        with mock.patch.object(dashboard, "_hosted_wording", side_effect=AssertionError("ran")):
            page = dashboard_html()
        for hosted in ("MEMORYSAFE CLOUD", "Manage on the web", "__ACCOUNT_URL__", "const accountUrl"):
            self.assertNotIn(hosted, page)
        self.assertIn("LOCAL · PRIVATE", page)

    def test_hosted_says_nothing_local(self) -> None:
        page = dashboard_html(hosted=True, account_url="https://memory.example/account")
        for local in ("this computer", "127.0.0.1", "LOCAL ·"):
            self.assertNotIn(local, page)
        # 0.4.27's "Move to Hosted" panel offers a local export through a relative fetch that
        # has no server behind it in a host's sandbox, and the user is already on Hosted.
        self.assertIn('id="hosted-panel" aria-label="Take your memory everywhere" hidden>', page)
        self.assertIn('id="hosted-export"', page)  # the script binds it; removing it would throw
        for hosted in ("MEMORYSAFE CLOUD", "Stored in MemorySafe cloud", "Manage on the web",
                       'href="https://memory.example/account"', "token_estimate", "Not available"):
            self.assertIn(hosted, page)

    def test_hosted_needs_an_account_url(self) -> None:
        with self.assertRaises(ValueError):
            dashboard_html(hosted=True)

    def test_local_does_not_need_an_account_url(self) -> None:
        self.assertIn("LOCAL · PRIVATE", dashboard_html(account_url=None))

    def test_account_url_is_json_escaped(self) -> None:
        # json.dumps leaves "</script>" and "><" alone, so a URL could close the script tag.
        page = dashboard_html(hosted=True, account_url='https://x.example/account"><script>')
        self.assertNotIn('"><script>', page)
        self.assertEqual(page.count("<script>"), 1)
        self.assertIn("\\u003cscript\\u003e", page)
