"""The plan limit: a full store stops accepting new memories and never removes one."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from memorysafe_chatgpt import plans, server, transfer
from memorysafe_chatgpt.storage import MemoryStore


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Signer:
    """What the account does when it issues a plan key."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = _b64(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def key(self, plan: str = plans.PAID, days: float = 35, size: int = 6) -> str:
        payload = _b64(json.dumps({"plan": plan, "max": size, "exp": time.time() + days * 86400}).encode())
        return f"{plans.KEY_PREFIX}.{payload}.{_b64(self.private.sign(payload.encode('ascii')))}"


class PlanKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = Signer()
        environment = patch.dict(os.environ, {"MEMORYSAFE_PLAN_PUBLIC_KEY": self.signer.public,
                                              "MEMORYSAFE_PLAN_FREE_LIMIT": "3"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_signed_unexpired_key_names_its_plan(self) -> None:
        self.assertEqual(plans.read_key(self.signer.key())["plan"], plans.PAID)

    def test_what_is_not_a_good_key_is_no_key(self) -> None:
        good = self.signer.key()
        prefix, payload, signature = good.split(".")
        forged = _b64(json.dumps({"plan": "paid", "max": 999999, "exp": time.time() + 9e9}).encode())
        for bad in ("", "nonsense", good[:-4] + "AAAA", f"{prefix}.{forged}.{signature}",
                    f"other.{payload}.{signature}", self.signer.key(days=-1), Signer().key(),
                    self.signer.key(size=0)):
            self.assertIsNone(plans.read_key(bad), bad[:30])

    def test_a_mistyped_key_is_never_stored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                plans.store_key(Path(folder), "msplan1.nope.nope")
            self.assertEqual(plans.stored_key(Path(folder)), "")
            plans.store_key(Path(folder), self.signer.key())
            if os.name != "nt":  # Windows keeps no owner-only mode bits; the folder's ACL governs
                self.assertEqual(oct((Path(folder) / plans.PLAN_FILE).stat().st_mode & 0o777), "0o600")
            self.assertTrue(plans.remove_key(Path(folder)))

    def test_no_public_key_or_no_free_size_means_no_local_limit(self) -> None:
        for missing in ({"MEMORYSAFE_PLAN_PUBLIC_KEY": ""}, {"MEMORYSAFE_PLAN_FREE_LIMIT": ""}):
            with patch.dict(os.environ, missing):
                self.assertFalse(plans.enforced())
                self.assertEqual(plans.local_plan(Path("/nowhere/data/x.sqlite3"))["limit"], None)
        # Both ship empty: setting them in a release is what turns the limit on, and no plan
        # size is published in this file before plans are announced.
        self.assertEqual(plans.PUBLIC_KEYS, ())
        self.assertIsNone(plans.FREE_MAX_ACTIVE)


class FullStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "memorysafe.sqlite3"
        self.state = self.root / "runtime-state"
        self.signer = Signer()
        for patcher in (
            patch.dict(os.environ, {"MEMORYSAFE_PLAN_PUBLIC_KEY": self.signer.public,
                                    "MEMORYSAFE_PLAN_FREE_LIMIT": "3",
                                    "MEMORYSAFE_STATE_DIR": str(self.state),
                                    "MEMORYSAFE_DB_PATH": str(self.database)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = MemoryStore(self.database)

    def fill(self, count: int = 3) -> list[str]:
        return [str(self.store.remember(f"Note {n}: the {word} gauge reads low", "other")["memory_id"])
                for n, word in zip(range(count), ("harbour", "river", "canal", "lake", "weir", "lock"))]

    def active(self) -> int:
        return self.store.health()["active_memories"]

    def test_a_full_store_refuses_a_new_memory_and_removes_nothing(self) -> None:
        ids = self.fill()
        refused = self.store.remember("The pump manual is on the top shelf", "other")
        self.assertEqual(refused["decision"], "FULL")
        self.assertIsNone(refused["memory_id"])
        self.assertIn("3 of 3", refused["reason"])
        self.assertIn("Nothing was removed", refused["reason"])
        self.assertNotIn("pump manual", refused["reason"])
        self.assertEqual(self.active(), 3)
        self.assertTrue(all(self.store.explain(one)["state"] == "active" for one in ids))
        self.assertEqual(self.store.find("pump manual", 5, record=False), [])
        self.assertEqual(self.store.health()["plan"], {"plan": "free", "used": 3, "limit": 3})

    def test_the_refusal_leaves_no_trace_of_the_text(self) -> None:
        self.fill()
        self.store.remember("The Quillfeather contract renews in March", "decision")
        blob = b"".join(path.read_bytes() for path in self.database.parent.glob("memorysafe.sqlite3*"))
        self.assertNotIn(b"Quillfeather", blob)
        self.assertEqual(self.store.health()["decision_counts"].get("FULL"), 1)

    def test_saying_it_again_and_correcting_it_still_work_when_full(self) -> None:
        self.store.remember("I live in Montreal", "personal")
        self.fill(2)
        again = self.store.remember("Note 0: the harbour gauge reads low", "other")
        self.assertEqual(again["decision"], "MERGE")
        moved = self.store.remember("I live in Ottawa", "personal")
        self.assertEqual(moved["lifecycle"], "superseded_previous", moved)
        self.assertEqual(self.active(), 3)

    def test_forgetting_makes_room(self) -> None:
        ids = self.fill()
        self.store.forget(ids[0], confirm=True)
        self.assertEqual(self.store.remember("The pump manual is on the top shelf", "other")["decision"], "STORE")

    def test_a_plan_key_raises_the_limit_and_an_expired_one_does_not(self) -> None:
        self.fill()
        plans.store_key(self.state, self.signer.key())
        self.assertEqual(self.store.remember("The pump manual is on the top shelf", "other")["decision"], "STORE")
        self.assertEqual(self.store.health()["plan"], {"plan": "paid", "used": 4, "limit": 6})
        (self.state / plans.PLAN_FILE).write_text(json.dumps({"key": self.signer.key(days=-1)}), encoding="utf-8")
        self.assertEqual(self.store.remember("The spare fuses are in the red tin", "other")["decision"], "FULL")

    def test_a_store_already_over_its_plan_keeps_everything(self) -> None:
        plans.store_key(self.state, self.signer.key())
        ids = self.fill(6)
        plans.remove_key(self.state)
        self.assertEqual(self.store.remember("One more for the road, the tin is red", "other")["decision"], "FULL")
        self.assertEqual(self.active(), 6)
        self.assertEqual(len(self.store.find("gauge reads low", 10, record=False)), 6)
        self.assertTrue(all(self.store.explain(one)["state"] == "active" for one in ids))

    def test_automatic_capture_says_it_was_not_saved(self) -> None:
        self.fill()
        self.store.set_automatic_mode(True)
        server._stores.clear()
        batch = server.automatic_capture(facts=["Robin owns the billing service"], category="project")
        self.assertEqual((batch.saved, batch.skipped), (0, 1))
        self.assertEqual(batch.results[0].decision, "FULL")
        self.assertFalse(batch.results[0].saved)

    def test_an_import_into_a_full_store_counts_what_did_not_fit(self) -> None:
        self.fill(2)
        result = transfer.import_memories(self.store, [
            {"content": "The pump manual is on the top shelf", "category": "other"},
            {"content": "The spare fuses are in the red tin", "category": "other"},
            {"content": "Note 0: the harbour gauge reads low", "category": "other"},
        ])
        self.assertEqual((result["stored"], result["merged"], result["failed"]), (1, 1, 1))
        self.assertIn("full", result["failures"][0]["reason"])

    def test_without_a_checkable_key_the_store_has_no_limit(self) -> None:
        with patch.dict(os.environ, {"MEMORYSAFE_PLAN_PUBLIC_KEY": ""}):
            store = MemoryStore(self.root / "data" / "free.sqlite3")
            for n in range(8):
                self.assertNotEqual(store.remember(f"Unlimited note number {n} about tide tables", "other")["decision"], "FULL")
            self.assertIsNone(store.health()["plan"])


class NearlyFullTests(unittest.TestCase):
    """A free store said nothing until the save that was refused. The person should hear about
    the limit while there is still room, without a note on every save for the last fifth of the
    plan (found on a clean install of 0.5.2, 17 Nov 2026)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.signer = Signer()
        environment = patch.dict(os.environ, {"MEMORYSAFE_PLAN_PUBLIC_KEY": self.signer.public,
                                              "MEMORYSAFE_PLAN_FREE_LIMIT": "20",
                                              "MEMORYSAFE_STATE_DIR": str(root / "runtime-state"),
                                              "MEMORYSAFE_DB_PATH": str(root / "data" / "memorysafe.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MemoryStore(root / "data" / "memorysafe.sqlite3")

    def save(self, n: int) -> dict:
        return self.store.remember(f"Gauge {n} on pier {n * 7 + 3} reads {n * 11 + 5} cm", "other")

    def test_once_at_eighty_percent_then_on_every_save_from_ninety(self) -> None:
        notes = [self.save(n).get("room_note") for n in range(20)]
        noted = [count for count, note in enumerate(notes, start=1) if note]
        self.assertEqual(noted, [16, 18, 19, 20])
        self.assertIn("16 of 20 memories on Free plan", notes[15])
        self.assertIn("nothing is ever removed", notes[15])
        self.assertIn("paid plan", notes[15])
        self.assertIn("20 of 20", notes[19])
        self.assertTrue(all("Gauge" not in note and "pier" not in note for note in notes if note))
        self.assertEqual(self.save(20)["decision"], "FULL")

    def test_the_thresholds(self) -> None:
        noted = [used for used in range(1, 151) if plans.nearly_full_note(used, 150, plans.FREE)]
        self.assertEqual(noted, [120] + list(range(135, 151)))
        self.assertIsNone(plans.nearly_full_note(151, 150, plans.FREE))
        self.assertIsNone(plans.nearly_full_note(5, 0, plans.FREE))
        self.assertNotIn("paid plan", plans.nearly_full_note(18, 20, plans.PAID))

    def test_the_assistant_receives_the_note(self) -> None:
        for n in range(15):
            self.save(n)
        sent = server.RememberResult.model_validate(self.save(15)).model_dump()
        self.assertIn("16 of 20", sent["room_note"])
        quiet = server.RememberResult.model_validate(self.save(16)).model_dump()
        self.assertNotIn("room_note", quiet)

    def test_a_store_with_no_limit_never_gets_the_note(self) -> None:
        with patch.dict(os.environ, {"MEMORYSAFE_PLAN_PUBLIC_KEY": ""}):
            store = MemoryStore(Path(self.temporary.name) / "data" / "open.sqlite3")
            for n in range(21):
                self.assertNotIn("room_note", store.remember(f"Open gauge {n} on pier {n * 7 + 3} reads {n * 11 + 5} cm", "other"))

if __name__ == "__main__":
    unittest.main()
