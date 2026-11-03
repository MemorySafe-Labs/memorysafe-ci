from __future__ import annotations

import unittest

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
