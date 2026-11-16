"""The two forms of memory text: what is stored (line breaks kept) and what is analysed (flat).

Until 0.4.31 remember() flattened every memory to one line, so a list or a heading never
reached the store. The stored form now keeps its lines; everything that pattern-matches
memory text reads the flat form, which is exactly what 0.4.30 stored.
"""

from __future__ import annotations

import unittest

from memorysafe_chatgpt.text import clean_text, flat


class CleanTextTests(unittest.TestCase):
    def test_one_line_is_flattened_as_before(self) -> None:
        self.assertEqual(clean_text("  I  live\tin   Montréal. "), "I live in Montréal.")

    def test_line_breaks_survive(self) -> None:
        self.assertEqual(clean_text("# Plan\n\n- one\n- two"), "# Plan\n\n- one\n- two")

    def test_every_line_ending_becomes_newline(self) -> None:
        # A Windows host sends \r\n; no \r may reach the store.
        self.assertEqual(clean_text("a\r\nb\rc\u2028d"), "a\nb\nc\nd")

    def test_leading_indentation_kept_inner_runs_collapsed_trailing_dropped(self) -> None:
        self.assertEqual(clean_text("- a\n    - b   c   \n"), "- a\n    - b c")

    def test_blank_runs_capped_and_edges_trimmed(self) -> None:
        self.assertEqual(clean_text("\n \n a\n\n\n\nb \n\n"), "a\n\nb")

    def test_whitespace_only_lines_become_empty(self) -> None:
        self.assertEqual(clean_text("a\n   \t\nb"), "a\n\nb")

    def test_only_whitespace_is_empty(self) -> None:
        self.assertEqual(clean_text(" \n\u2028\r\n\t"), "")

    def test_flatten_invariant(self) -> None:
        for sample in ["a  b", "# h\n\n- x\n  - y", "a\r\n\r\n\r\nb", "\u2028x\u2029 y\t", "", "\x0bv\x0c"]:
            with self.subTest(sample=sample):
                self.assertEqual(" ".join(clean_text(sample).split()), " ".join(sample.split()))

    def test_flat(self) -> None:
        self.assertEqual(flat("# Plan\n\n- one\n  - two "), "# Plan - one - two")


if __name__ == "__main__":
    unittest.main()
