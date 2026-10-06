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
