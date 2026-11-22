"""What automatic capture refuses, and what it must not refuse.

The negative cases carry more weight than the positive ones. A detector that
fires on version numbers, ports, dates or dollar amounts makes automatic capture
useless, gets switched off, and costs every memory rather than one. A reviewer made
adversarial negatives a condition of this change; the ALLOW list is that.
"""

from __future__ import annotations

import unittest

from memorysafe_chatgpt.capture_policy import classify, reason_for, skip_reason

REFUSED = [
    ("credential", "The key is sk_live_ABCDEFGHIJKLMNOPQRSTUV0123"),
    ("credential", "token eyJhbGciOiJIUzI1NiXX.eyJzdWIiOiIxMjM0NTY.SflKxwRJSMeKKF2QT4"),
    ("credential", "-----BEGIN RSA PRIVATE KEY-----"),
    ("payment_card", "Card 4111 1111 1111 1111 on file"),
    ("payment_card", "use 5500-0000-0000-0004 for the subscription"),
    ("government_id", "SIN 046 454 286 for the payroll form"),
    ("government_id", "SSN 123-45-6789"),
    ("email_address", "Jordan's email is someone@example.com"),
    ("phone_number", "Call the office at (514) 555-0142"),
    ("phone_number", "reach me on +1 438-555-0199"),
    ("street_address", "Dana lives at 1200 Example Street"),
    ("street_address", "the office is 400 Boulevard de l'Exemple"),
    ("postal_code", "mail it to H3A 1B2"),
    ("sensitive_subject", "my password is the usual one"),
    ("sensitive_subject", "I have an allergy I should mention"),
]

# Each of these is a real shape from this project's own store or source.
ALLOWED = [
    "Beta 0.3.7 fixed the Windows manifest and the 21s cold initialize",
    "MemorySafe Labs incorporated 13 July 2026",
    "The enterprise number is 1182359472",
    "The dashboard listens on 127.0.0.1:8765",
    "Primo-adoptants closes 4 November 2026 at 5pm",
    "The Gama resubmission deadline is 28 September 2026",
    "The pitch grant is CAD 25,000 non-dilutive",
    "Pneumonia held-out combined AUPRC 0.702 plus or minus 0.041 versus 0.687",
    "Device id MS-9A1E0569 identifies this install",
    "Conflict 20 is open and needs review",
    "SHA-256 fd3217afab237825f2d3bb7170d06ddc18c57d1b4239ca031fa53adcb65d0e01",
    "Apple Team S274J7998P is enrolled as Individual",
    "Dana prefers straight talk over cheerleading",
    "Avery Stone is EVP of operations at a regional cooperative",
    "Suite 300 is where the meeting happened",
    "Highway 40 was closed",
    "The call is at 11:00 on 2026-07-28",
    "Version 2024-11-05 of the protocol",
    "Sam reached the shared store via local execution on 2026-09-11",
]


class RefusalTests(unittest.TestCase):
    def test_each_sensitive_shape_is_refused_and_named(self) -> None:
        for expected, content in REFUSED:
            with self.subTest(content=content):
                self.assertEqual(classify(content), expected)

    def test_ordinary_project_facts_are_not_refused(self) -> None:
        for content in ALLOWED:
            with self.subTest(content=content):
                self.assertIsNone(classify(content), f"false refusal: {content}")

    def test_a_refusal_reason_never_echoes_the_refused_value(self) -> None:
        # Two reviewers both landed on this independently. A reason is stored in
        # decision_events and shown in governance output, so a reason that quotes
        # the secret re-leaks it into the log meant to prove we did not keep it.
        secrets = [
            "sk_live_ABCDEFGHIJKLMNOPQRSTUV0123",
            "4111 1111 1111 1111",
            "someone@example.com",
            "(514) 555-0142",
            "1200 Example Street",
            "046 454 286",
            "H3A 1B2",
        ]
        for _, content in REFUSED:
            shape = classify(content)
            assert shape is not None
            reason = reason_for(shape)
            for secret in secrets:
                self.assertNotIn(secret, reason)
            # Nor any long fragment of the message itself.
            for word in content.split():
                if len(word) > 12:
                    self.assertNotIn(word, reason)

    def test_the_label_only_bug_stays_fixed(self) -> None:
        # The old guard had this exactly backwards: it refused the phrase and
        # saved the value. The value must now be refused.
        self.assertEqual(
            classify("Jordan's email is someone@example.com"), "email_address"
        )
        # And the bare phrase is deliberately allowed. This is the design
        # decision the failure of an earlier version of this test forced me to
        # state: for contact and location, the value is the risk and the phrase
        # is not, so refusing "do not store my email address" is a false skip
        # that teaches the user the filter is superstitious.
        self.assertIsNone(classify("do not store my email address"))
        self.assertIsNone(classify("send me your phone number when you can"))
        # Credentials, identity documents and health are the exception: there the
        # subject itself is the signal, because the surrounding content is
        # usually sensitive even when no value is present in this message.
        self.assertEqual(classify("my password is the usual one"), "sensitive_subject")
        self.assertEqual(classify("bring your passport"), "sensitive_subject")

    def test_luhn_stops_long_numbers_that_are_not_cards(self) -> None:
        self.assertIsNone(classify("order 1234567890123456 shipped"))
        self.assertEqual(classify("card 4111111111111111"), "payment_card")

    def test_mode_off_and_safety_and_length_are_reported_separately(self) -> None:
        self.assertEqual(skip_reason("anything", "other", False)[0], "automatic_mode_off")
        self.assertEqual(skip_reason("anything", "safety", True)[0], "safety_category")
        self.assertEqual(skip_reason("x" * 501, "other", True)[0], "too_long")
        self.assertIsNone(skip_reason("Beta 0.3.7 shipped on Windows", "project", True))

    def test_every_shape_has_its_own_reason(self) -> None:
        shapes = {shape for shape, _ in REFUSED}
        reasons = {reason_for(shape) for shape in shapes}
        self.assertEqual(len(reasons), len(shapes), "two shapes share a reason string")


if __name__ == "__main__":
    unittest.main()


class CredentialShapeTests(unittest.TestCase):
    """Formats the first pattern let through (10 Nov 2026). The importer shares this module
    as its only gate before a file is uploaded to a hosted account."""

    def test_issuer_prefixed_secrets_are_refused(self) -> None:
        for content in (
            "the key id is AKIAIOSFODNN7EXAMPLE",
            "use github_pat_11ABCDEFG0abcdefghijklmnopqrstuv for the release bot",
            "the maps key is AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "send Authorization: Bearer abcdefghijklmnop1234 with each call",
            "the bot token is xoxa-2-1234567890-abcdefghij",
            "publish with npm_abcdefghijklmnopqrstuvwxyz0123456789",
            "the database is postgres://admin:s3cr3t@db.internal:5432/prod",
        ):
            with self.subTest(content=content):
                self.assertEqual(classify(content), "credential")

    def test_ordinary_technical_sentences_are_not_credentials(self) -> None:
        for content in (
            "We use sk-learn for the baseline model",
            "The API base is https://api.example.com/v2",
            "Clone from https://github.com/example/repo.git",
            "The bearer of the badge gets building access",
            "AKIA is the prefix AWS uses for key ids",
            "Connect to postgres://db.internal:5432/prod through the tunnel",
        ):
            with self.subTest(content=content):
                self.assertIsNone(classify(content), f"false refusal: {content}")


class StreetShapeTests(unittest.TestCase):
    def test_a_number_and_a_road_word_are_not_an_address_without_a_name(self) -> None:
        # Each was refused as a street address; a detector that fires on ordinary project
        # sentences gets automatic mode switched off.
        for content in (
            "There are 2 ways to drive the build",
            "Release 3 is on the road map for Q1",
            "The site is 5 minutes drive from the office",
            "We run 12 services in the court booking app",
            "Keep 3 lanes of work in the place we agreed",
        ):
            with self.subTest(content=content):
                self.assertIsNone(classify(content), f"false refusal: {content}")

    def test_named_streets_are_still_refused_in_both_word_orders(self) -> None:
        for content in (
            "She lives at 1 Example Street, Montreal",
            "the office is 400 Boulevard de l'Exemple",
            "send it to 12 rue Sainte-Catherine Ouest",
            "4567 boul. Saint-Laurent is the new address",
            "the studio is at 221B Baker Street",
            "meet at 40 5th Avenue",
        ):
            with self.subTest(content=content):
                self.assertEqual(classify(content), "street_address")


class PassingRemarkTests(unittest.TestCase):
    """remark(): what automatic capture declines because it is not a lasting fact."""

    def test_each_shape_is_named(self) -> None:
        from memorysafe_chatgpt.capture_policy import remark

        for expected, content in (
            ("question", "What time is the demo?"),
            ("acknowledgement", "ok thanks"),
            ("acknowledgement", "Never mind, I found it."),
            ("about_the_chat", "We discussed the migration plan"),
            ("about_the_chat", "I updated the README and fixed the typo"),
            ("about_the_chat", "The user asked me to shorten the introduction"),
            ("assistant_guess", "The user seems curious about the roadmap"),
            ("passing_state", "I'm sleepy today"),
            ("passing_state", "I'm debugging the export script at the moment"),
            ("passing_state", "We decided to ship tomorrow"),
            ("one_off", "Don't add tests yet."),
            ("one_off", "Use the staging database for this run, not prod."),
            ("one_off", "let's give that another go"),
            ("one_off", "make this heading bolder"),
        ):
            with self.subTest(content=content):
                self.assertEqual(remark(content), expected)

    def test_lasting_facts_and_standing_rules_are_kept(self) -> None:
        from memorysafe_chatgpt.capture_policy import remark

        for content in (
            "Always use Black for formatting",
            "I prefer pytest over unittest",
            "From now on, no new dependencies",
            "Starting today we use pnpm instead of npm",
            "argh, no more Monday releases, ever",
            "Never deploy on Fridays",
            "Call me Sam",
            "The grant deadline is 4 November 2026",
            "We use Postgres 17 in production",
            "Write commit messages in the imperative mood",
            "Use pnpm instead of npm",
            "Robin owns the billing service",
            "Thanks to the grant, we always prototype first",
            "The user prefers tabs over spaces",
            "Je préfère toujours les réponses courtes",
        ):
            with self.subTest(content=content):
                self.assertIsNone(remark(content), f"false refusal: {content}")

    def test_a_remark_is_refused_by_automatic_capture_and_the_reason_holds_no_text(self) -> None:
        from memorysafe_chatgpt.capture_policy import REMARK_SHAPES, skip_reason

        decision = skip_reason("I'm rewriting the Quillfeather invoice template right now", "task", True)
        self.assertIsNotNone(decision)
        shape, reason = decision
        self.assertIn(shape, REMARK_SHAPES)
        self.assertNotIn("Quillfeather", reason)

    def test_a_sensitive_shape_is_named_before_a_remark(self) -> None:
        from memorysafe_chatgpt.capture_policy import skip_reason

        self.assertEqual(skip_reason("is my card 4111 1111 1111 1111 still valid?", "other", True)[0], "payment_card")


class FoundInReviewTests(unittest.TestCase):
    """Corrections made on 10 Nov 2026 from errors found in review, after the release's
    measurements were taken. These tests are what holds them."""

    def test_a_chatty_opening_does_not_make_a_fact_an_acknowledgement(self) -> None:
        from memorysafe_chatgpt.capture_policy import remark

        for content in (
            "no meetings on Wednesdays. ever.",
            "haha please stop writing utilize",
            "right, decision made: the report goes out quarterly.",
            "no new dependencies",
            "ok so the deadline is 4 March 2027",
        ):
            with self.subTest(content=content):
                self.assertIsNone(remark(content))
        for content in ("ok thanks", "sounds good to me", "Never mind, it's sorted now.", "nope", "great, thanks"):
            with self.subTest(content=content):
                self.assertEqual(remark(content), "acknowledgement")

    def test_a_tag_question_is_not_a_question(self) -> None:
        from memorysafe_chatgpt.capture_policy import remark

        self.assertIsNone(remark("no anglicisms in the copy, ok?"))
        self.assertIsNone(remark("sem jargão nos relatórios, ok?"))
        self.assertEqual(remark("is the copy ok?"), "question")

    def test_health_said_other_ways_is_refused(self) -> None:
        for content in (
            "I take 20 mg of atorvastatin each night.",
            "I am pregnant, so no night shifts until August.",
            "I had shoulder surgery in June and still can't lift much.",
            "Mon médecin m'a mis sous traitement pour le cholestérol.",
            "Tenho diabetes e tomo insulina todos os dias.",
        ):
            with self.subTest(content=content):
                self.assertEqual(classify(content), "sensitive_subject")
        for content in ("The surgery scheduling module ships in May", "Each tablet holds 50 mg of buffer in the lab protocol",
                        "The asthma study dashboard is owned by the data team"):
            with self.subTest(content=content):
                self.assertIsNone(classify(content), f"false refusal: {content}")

    def test_more_identifiers_and_secret_addresses_are_refused(self) -> None:
        for expected, content in (
            ("government_id", "My IBAN is DE89 3704 0044 0532 0130 00."),
            ("government_id", "Meu CPF é 529.982.247-25."),
            ("credential", "Deploy webhook: https://hooks.slack.example/services/T11111111/B22222222/YYYYYYYYYYYYYYYYYYYYYYYY"),
            ("credential", "The staging login for the wiki is editor / Tr1al!-sample."),
        ):
            with self.subTest(content=content):
                self.assertEqual(classify(content), expected)
        for content in ("The login page is at /account/start", "Build AB12 CDEF passed on the second run",
                        "Docs are at https://example.com/guides/webhooks"):
            with self.subTest(content=content):
                self.assertIsNone(classify(content), f"false refusal: {content}")
