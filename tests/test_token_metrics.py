"""The token model behind memorysafe_health.

storage.token_benchmark imported three names this module did not define, so every
health call raised ImportError and the tool never answered at all.
"""

from __future__ import annotations

import unittest

from memorysafe_chatgpt.bootstrap_catalog import SERVER_INSTRUCTIONS
from memorysafe_chatgpt.token_metrics import (
    RECALLED_PER_TURN,
    break_even_facts,
    count_text_tokens,
    fixed_overhead_tokens,
)


class TokenModelTests(unittest.TestCase):
    def test_overhead_is_the_sum_of_what_every_conversation_is_sent(self) -> None:
        overhead = fixed_overhead_tokens()
        self.assertGreater(overhead["tool_schema_tokens"], 0)
        self.assertEqual(overhead["instruction_tokens"], count_text_tokens(SERVER_INSTRUCTIONS))
        self.assertEqual(
            overhead["fixed_overhead_tokens"],
            overhead["tool_schema_tokens"] + overhead["instruction_tokens"],
        )

    def test_break_even_is_the_smallest_store_that_pays_for_the_overhead(self) -> None:
        fixed = fixed_overhead_tokens()["fixed_overhead_tokens"]
        for average in (1, 7, 60, 400):
            facts = break_even_facts(average)
            governed = fixed + RECALLED_PER_TURN * average
            self.assertGreaterEqual(facts * average, governed, average)
            self.assertLess((facts - 1) * average, governed, average)


if __name__ == "__main__":
    unittest.main()
