"""The legal documents describe the product that actually exists.

They were written in August for a ChatGPT-only beta and said so throughout: that
ChatGPT sends every request through OpenAI's tunnel, that the user's ChatGPT account is
the one to protect, that disconnecting means disconnecting from ChatGPT. By 0.4 the
assistants are Claude Code, Claude Desktop and Codex, reaching one local database on the
same computer, with ChatGPT a macOS-only case through the tunnel. A privacy policy that
misdescribes where content goes is worse than a vague one, so these pin the corrections.

They are not a substitute for legal review. What they check is factual accuracy about
the software and that the consent record names versions the user was actually shown.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from memorysafe_chatgpt.setup_app import (
    PRIVACY_VERSION,
    TERMS_VERSION,
    accepted_the_current_documents,
)


LEGAL = Path(__file__).resolve().parents[1] / "legal"
TERMS = LEGAL / "MemorySafe_Beta_Terms_of_Use.md"
PRIVACY = LEGAL / "MemorySafe_Beta_Privacy_Policy.md"
CONSENT = LEGAL / "MemorySafe_Beta_Consent_Copy.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _version(text: str) -> str:
    found = re.search(r"\*\*Version:\*\*\s*([0-9]+\.[0-9]+)", text)
    assert found is not None, "the document states no version"
    return found.group(1)


class VersionTests(unittest.TestCase):
    def test_the_consent_record_names_the_versions_the_user_was_shown(self) -> None:
        """consent.json stores these strings. A record naming a version nobody was shown
        says nothing about what was agreed to, and they were hardcoded apart from the
        documents until the documents moved and they did not."""
        self.assertEqual(_version(_read(TERMS)), TERMS_VERSION)
        self.assertEqual(_version(_read(PRIVACY)), PRIVACY_VERSION)

    def test_the_consent_copy_moves_with_them(self) -> None:
        self.assertIn(f"**Version:** {TERMS_VERSION}", _read(CONSENT))


class AssistantAccuracyTests(unittest.TestCase):
    def test_every_supported_assistant_is_named(self) -> None:
        for path in (TERMS, PRIVACY):
            text = _read(path)
            for assistant in ("Claude Code", "Claude Desktop", "Codex"):
                self.assertIn(assistant, text, f"{path.name} does not mention {assistant}")

    def test_chatgpt_is_described_as_the_macos_tunnel_case(self) -> None:
        """Still supported, no longer the whole product. It is the one host whose traffic
        leaves the computer to reach MemorySafe, so it is the one that needs saying."""
        for path in (TERMS, PRIVACY):
            text = _read(path)
            self.assertIn("ChatGPT", text, path.name)
            self.assertIn("tunnel", text, path.name)

    def test_neither_document_claims_every_request_goes_through_openai(self) -> None:
        """The old wording was flatly wrong for Claude Code, Claude Desktop and Codex,
        whose requests never leave the machine on the way to the connector."""
        for path in (TERMS, PRIVACY, CONSENT):
            text = _read(path)
            for claim in (
                "processed through ChatGPT and OpenAI",
                "These transmissions pass through OpenAI's systems.",
                "is transmitted through and processed by OpenAI.",
            ):
                self.assertNotIn(claim, text, f"{path.name} still says: {claim}")

    def test_the_local_case_is_stated_and_not_only_the_tunnel(self) -> None:
        privacy = _read(PRIVACY)
        self.assertIn("travel locally between it and the connector", privacy)
        self.assertIn("these transmissions are local to the computer", privacy.lower())

    def test_both_providers_are_named_where_content_reaches_them(self) -> None:
        """Memory content does reach a provider -- through the assistant's own
        conversation, not through the connector. Saying only OpenAI left Anthropic, who
        serves two of the three assistants, unmentioned."""
        for path in (TERMS, PRIVACY):
            text = _read(path)
            self.assertIn("Anthropic", text, path.name)
            self.assertIn("OpenAI", text, path.name)


class ReacceptanceTests(unittest.TestCase):
    """Section 15 promises reasonable notice of material changes.

    The documents moved to 0.2 when they were corrected. Nothing compared the recorded
    version with the current one, so everyone who accepted 0.1 would have stayed recorded
    as having accepted 0.1 for good, and the promise would have gone unkept by default.
    """

    def test_an_acceptance_of_the_current_documents_stands(self) -> None:
        self.assertTrue(accepted_the_current_documents({
            "terms_accepted": True,
            "terms_version": TERMS_VERSION,
            "privacy_version": PRIVACY_VERSION,
        }))

    def test_an_acceptance_of_older_documents_is_asked_again(self) -> None:
        self.assertFalse(accepted_the_current_documents({
            "terms_accepted": True, "terms_version": "0.1", "privacy_version": "0.1",
        }))

    def test_a_privacy_policy_that_moved_alone_is_enough_to_ask_again(self) -> None:
        self.assertFalse(accepted_the_current_documents({
            "terms_accepted": True, "terms_version": TERMS_VERSION, "privacy_version": "0.1",
        }))

    def test_a_record_from_before_versions_were_stored_is_asked_again(self) -> None:
        self.assertFalse(accepted_the_current_documents({"terms_accepted": True}))

    def test_nothing_accepted_is_still_nothing_accepted(self) -> None:
        self.assertFalse(accepted_the_current_documents({}))
        self.assertFalse(accepted_the_current_documents({
            "terms_accepted": False, "terms_version": TERMS_VERSION, "privacy_version": PRIVACY_VERSION,
        }))

    def test_the_page_says_why_it_is_asking(self) -> None:
        """Reopening the step without a word looks like the answer was lost."""
        page = Path(__file__).resolve().parents[1] / "src" / "memorysafe_chatgpt" / "setup_app.py"
        text = page.read_text(encoding="utf-8")
        self.assertIn('id="terms-update"', text)
        self.assertIn("s.terms_update_pending", text)
        self.assertIn("Nothing has stopped working", text)


class StaleClaimTests(unittest.TestCase):
    def test_no_hardcoded_beta_version_in_the_privacy_policy(self) -> None:
        """It said "In Beta version 0.2.0, the Forget action ..." while shipping 0.4.x."""
        self.assertNotIn("Beta version 0.2.0", _read(PRIVACY))

    def test_the_terms_name_the_uninstaller_that_now_exists(self) -> None:
        self.assertIn("memorysafe uninstall", _read(TERMS))


if __name__ == "__main__":
    unittest.main()
