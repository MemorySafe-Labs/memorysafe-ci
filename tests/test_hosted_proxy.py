"""Hosted mode's proxy: the same tools, answered by the person's hosted account.

What these tests pin down: nothing leaves this computer in local mode or without a tool call;
in hosted mode each local tool forwards its own name and arguments once, with the bearer, and
returns the account's result in the local shape; every failure is one of the honest sentences
(not linked, sign in again, offline, unsure) or the account's own words; a token the account
stopped honouring is refreshed once and never looped; a write that timed out is never retried;
the hosted edition itself (a store provider) never forwards; /api/export refuses in hosted mode.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import client_identity, device_link, hosted_proxy, server, storage_mode
from memorysafe_chatgpt.device import utc_now, write_private_json
from memorysafe_chatgpt.storage import MemoryStore

ORIGIN = "https://memory.test"


def write_link(state_dir: Path, *, expires_in: float = 3600, access_token: str = "access-1") -> None:
    write_private_json(
        device_link.link_path(state_dir),
        {
            "issuer": ORIGIN,
            "token_endpoint": f"{ORIGIN}/token",
            "revocation_endpoint": f"{ORIGIN}/revoke",
            "client_id": client_identity.CLIENT_ID,
            "scope": "memories offline_access",
            "access_token": access_token,
            "access_expires_at": time.time() + expires_in,
            "refresh_token": "refresh-1",
            "linked_at": utc_now(),
        },
    )


class FakeAccount:
    """The hosted edition's /mcp as a PostCall: a throwaway local engine behind JSON-RPC.

    The real hosted edition runs the same engine over Postgres, so the shapes it returns are
    the shapes this fake returns. Failure modes are switches."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.requests: list[tuple[str, dict, dict]] = []
        self.unauthorized_times = 0
        self.status: int | None = None
        self.raise_error: BaseException | None = None
        self.answer: bytes | None = None
        self.tool_error: str | None = None

    def __call__(self, url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        message = json.loads(body)
        self.requests.append((url, message, headers))
        if self.raise_error is not None:
            raise self.raise_error
        if self.unauthorized_times > 0:
            self.unauthorized_times -= 1
            return 401, b'{"error": "invalid_token"}'
        if self.status is not None:
            return self.status, b"unavailable"
        if self.answer is not None:
            return 200, self.answer
        name = message["params"]["name"]
        arguments = message["params"]["arguments"]
        if self.tool_error is not None:
            result = {"content": [{"type": "text", "text": f"Error executing tool {name}: {self.tool_error}"}], "isError": True}
        else:
            payload = self.serve(name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload, "isError": False}
        return 200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode("utf-8")

    def serve(self, name: str, arguments: dict) -> dict:
        store = self.store
        if name == "memorysafe_remember":
            return store.remember(arguments["content"], arguments.get("category", "other"))
        if name == "memorysafe_set_auto_mode":
            return store.set_automatic_mode(arguments["enabled"])
        if name == "memorysafe_auto_capture":
            results = []
            for fact in arguments["facts"]:
                stored = store.remember(fact, arguments.get("category", "other"), source="automatic")
                results.append({"saved": True, "decision": stored["decision"], "memory_id": stored["memory_id"],
                                "content": stored["content"], "category": stored["category"], "reason": stored["reason"]})
            return {"saved": len(results), "skipped": 0, "results": results}
        if name == "memorysafe_find":
            matches = store.find(arguments.get("query", ""), arguments.get("limit", 5))
            return {"query": arguments.get("query", ""), "count": len(matches), "memories": matches}
        if name == "memorysafe_forget":
            return store.forget(arguments["memory_id"], confirm=arguments.get("confirm", False))
        if name == "memorysafe_protect":
            return store.protect(arguments["memory_id"], protected=arguments.get("protect", True))
        if name == "memorysafe_explain":
            return store.inspect_memory(arguments["memory_id"])
        if name == "memorysafe_review_conflicts":
            return store.review_conflicts(include_resolved=arguments.get("include_resolved", False))
        if name == "memorysafe_resolve_conflict":
            return store.resolve_conflict(arguments["conflict_id"], arguments["action"], confirm=arguments.get("confirm", False))
        if name == "memorysafe_restore":
            return store.restore(arguments["memory_id"], confirm=arguments.get("confirm", False))
        if name == "memorysafe_health":
            # What private catalog.hosted_health sends: the local fields minus this computer's,
            # plus stored_in and a token_estimate of kind hosted_estimate.
            data = store.health()
            data["status"] = "healthy"
            data["recent_memories"] = store.recent(6)
            data["stored_in"] = "Stored in your MemorySafe account"
            estimate = dict(data.pop("live_token_metrics"))
            estimate["kind"] = "hosted_estimate"
            data["token_estimate"] = estimate
            for local_only in ("database_path", "dashboard_url", "opened", "max_active"):
                data.pop(local_only, None)
            return data
        raise AssertionError(f"the fake account has no tool {name}")


class ProxyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.account_store = MemoryStore(root / "account.sqlite3")
        self.account = FakeAccount(self.account_store)
        self.refresher = FakeRefresh()
        http_patch = patch.object(device_link, "_http", self.refresher)
        http_patch.start()
        self.addCleanup(http_patch.stop)

    def call(self, name: str, arguments: dict) -> dict:
        return hosted_proxy.call(name, arguments, state_dir=self.state_dir, post=self.account)


class FakeRefresh:
    """The account's token endpoint, for the one refresh the proxy may ask for."""

    def __init__(self) -> None:
        self.calls = 0
        self.refuse = False

    def __call__(self, url: str, form: dict | None, headers: dict) -> tuple[int, dict]:
        self.calls += 1
        if self.refuse:
            return 400, {"error": "invalid_grant"}
        return 200, {"access_token": f"access-refreshed-{self.calls}", "token_type": "Bearer", "expires_in": 3600,
                     "refresh_token": f"refresh-{self.calls + 1}", "scope": "memories offline_access"}


class CallTests(ProxyBase):
    def test_not_signed_in_is_the_not_linked_sentence_and_no_request(self) -> None:
        with self.assertRaises(device_link.LinkError) as caught:
            self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(str(caught.exception), device_link.NOT_LINKED)
        self.assertEqual(self.account.requests, [])

    def test_a_call_is_one_tools_call_with_the_bearer_and_returns_the_structured_result(self) -> None:
        write_link(self.state_dir)
        result = self.call("memorysafe_remember", {"content": "The team prefers plain answers", "category": "preference"})
        self.assertIn(result["decision"], ("STORE", "PROTECT"))
        self.assertEqual(len(self.account.requests), 1)
        url, message, headers = self.account.requests[0]
        self.assertEqual(url, f"{ORIGIN}/mcp")
        self.assertEqual(message["method"], "tools/call")
        self.assertEqual(message["params"], {"name": "memorysafe_remember",
                                             "arguments": {"content": "The team prefers plain answers", "category": "preference"}})
        self.assertEqual(headers["Authorization"], "Bearer access-1")
        self.assertEqual(headers["MCP-Protocol-Version"], hosted_proxy.PROTOCOL_VERSION)
        self.assertIn("application/json", headers["Accept"])
        self.assertEqual(self.refresher.calls, 0)

    def test_a_token_about_to_expire_is_refreshed_before_the_call(self) -> None:
        write_link(self.state_dir, expires_in=10)
        self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(self.refresher.calls, 1)
        self.assertEqual(self.account.requests[0][2]["Authorization"], "Bearer access-refreshed-1")

    def test_dropping_a_spent_token_never_writes_back_a_spent_refresh_token(self) -> None:
        """A 401 in one process while another refreshes: the copy read before the refresh
        held a refresh token already used. Writing it back made the next refresh a replay,
        and a replay signs the person out on every computer."""
        write_link(self.state_dir, access_token="access-1")
        # Another process refreshed: new access token, new refresh token.
        record = device_link.read_link(self.state_dir)
        record.update({"access_token": "access-2", "refresh_token": "refresh-2"})
        write_private_json(device_link.link_path(self.state_dir), record)
        device_link.forget_access_token(self.state_dir, spent="access-1")
        after = device_link.read_link(self.state_dir)
        self.assertEqual((after["access_token"], after["refresh_token"]), ("access-2", "refresh-2"))
        # The token that was in fact refused is dropped, and the live refresh token kept.
        device_link.forget_access_token(self.state_dir, spent="access-2")
        after = device_link.read_link(self.state_dir)
        self.assertNotIn("access_token", after)
        self.assertEqual(after["refresh_token"], "refresh-2")

    def test_a_token_the_account_stopped_honouring_is_refreshed_once_then_retried(self) -> None:
        write_link(self.state_dir)
        self.account.unauthorized_times = 1
        result = self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(result["count"], 0)
        self.assertEqual([h["Authorization"] for _u, _m, h in self.account.requests],
                         ["Bearer access-1", "Bearer access-refreshed-1"])
        self.assertTrue(device_link.read_link(self.state_dir))

    def test_a_refreshed_token_still_refused_is_sign_in_again_and_never_a_loop(self) -> None:
        write_link(self.state_dir)
        self.account.unauthorized_times = 5
        with self.assertRaises(device_link.LinkError) as caught:
            self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(str(caught.exception), device_link.SIGN_IN_AGAIN)
        self.assertEqual(len(self.account.requests), 2)
        self.assertEqual(self.refresher.calls, 1)

    def test_a_refresh_the_account_refuses_deletes_the_link_and_says_sign_in_again(self) -> None:
        write_link(self.state_dir, expires_in=0)
        self.refresher.refuse = True
        with self.assertRaises(device_link.LinkError) as caught:
            self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(str(caught.exception), device_link.SIGN_IN_AGAIN)
        self.assertFalse(device_link.read_link(self.state_dir))
        self.assertEqual(self.account.requests, [])

    def test_the_account_being_unreachable_is_the_offline_sentence_and_keeps_the_link(self) -> None:
        write_link(self.state_dir)
        for error in (ConnectionRefusedError("refused"), urllib.error.URLError(OSError("no route")), OSError("dns")):
            self.account.raise_error = error
            with self.assertRaises(device_link.LinkError) as caught:
                self.call("memorysafe_remember", {"content": "x", "category": "other"})
            self.assertEqual(str(caught.exception), device_link.OFFLINE, repr(error))
        self.account.raise_error = None
        self.account.status = 503
        with self.assertRaises(device_link.LinkError) as caught:
            self.call("memorysafe_find", {"query": "", "limit": 1})
        self.assertEqual(str(caught.exception), device_link.OFFLINE)
        self.assertTrue(device_link.read_link(self.state_dir))

    def test_a_write_that_timed_out_is_unsure_not_not_changed_and_is_not_retried(self) -> None:
        write_link(self.state_dir)
        for error in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            self.account.requests.clear()
            self.account.raise_error = error
            with self.assertRaises(hosted_proxy.HostedError) as caught:
                self.call("memorysafe_remember", {"content": "x", "category": "other"})
            self.assertEqual(str(caught.exception), hosted_proxy.UNSURE, repr(error))
            self.assertEqual(len(self.account.requests), 1)

    def test_a_write_whose_answer_was_lost_is_unsure_and_a_read_is_offline(self) -> None:
        """A connection reset, dropped or cut short after the request went out says nothing
        about whether the account saved it. Only a failure before anything was sent is "not
        changed"."""
        import http.client

        write_link(self.state_dir)
        lost = (ConnectionResetError("reset"), http.client.RemoteDisconnected("closed"),
                http.client.IncompleteRead(b"half"), BrokenPipeError("pipe"),
                urllib.error.URLError(ConnectionResetError("reset")))
        for error in lost:
            self.account.requests.clear()
            self.account.raise_error = error
            with self.assertRaises(hosted_proxy.HostedError) as caught:
                self.call("memorysafe_remember", {"content": "x", "category": "other"})
            self.assertEqual(str(caught.exception), hosted_proxy.UNSURE, repr(error))
            self.assertEqual(len(self.account.requests), 1)
            with self.assertRaises(device_link.LinkError) as offline:
                self.call("memorysafe_find", {"query": "", "limit": 1})
            self.assertEqual(str(offline.exception), device_link.OFFLINE, repr(error))

    def test_an_unreadable_yes_to_a_write_is_unsure(self) -> None:
        write_link(self.state_dir)

        def garbled(url, body, headers):
            return 200, b"<html>not what this plugin reads</html>"

        with self.assertRaises(hosted_proxy.HostedError) as caught:
            hosted_proxy.call("memorysafe_remember", {"content": "x", "category": "other"},
                              state_dir=self.state_dir, post=garbled)
        self.assertEqual(str(caught.exception), hosted_proxy.UNSURE_UNREADABLE)
        with self.assertRaises(hosted_proxy.HostedError) as read:
            hosted_proxy.call("memorysafe_find", {"query": "", "limit": 1}, state_dir=self.state_dir, post=garbled)
        self.assertEqual(str(read.exception), hosted_proxy.UNREADABLE)

    def test_a_call_with_a_token_follows_no_redirect(self) -> None:
        """urllib resends the Authorization header to wherever a 302 points, plain http
        included. The account's endpoints do not redirect, so none is followed."""
        handlers = [type(handler) for handler in device_link._OPENER.handlers]
        self.assertIn(device_link._NoRedirect, handlers)
        self.assertNotIn(urllib.request.HTTPRedirectHandler, handlers)
        request = urllib.request.Request("https://memory.example/mcp")
        self.assertIsNone(device_link._NoRedirect().redirect_request(request, None, 302, "Found", {}, "http://evil.example/"))

    def test_the_accounts_out_of_date_flags_reach_the_assistant(self) -> None:
        """The hosted account marks a found memory that may be out of date. The plugin read
        the answer into its own result model, which dropped the keys it did not declare, so
        the assistant stated an old status as current."""
        write_link(self.state_dir)
        answer = {"query": "kestrel audit", "count": 1, "memories": [{
            "memory_id": "MS-1", "content": "The Kestrel audit is pending.", "category": "project",
            "updated_at": "2026-10-01T00:00:00+00:00", "layer": "project:kestrel",
            "possibly_stale": True, "stale_reason": "A later note names the Kestrel audit.",
        }]}
        with patch.object(hosted_proxy, "call", return_value=answer):
            result = server._forward("memorysafe_find", server.FindResult, query="kestrel audit", limit=5)
        sent = result.model_dump()["memories"][0]
        self.assertTrue(sent["possibly_stale"])
        self.assertEqual(sent["stale_reason"], "A later note names the Kestrel audit.")
        self.assertEqual(sent["layer"], "project:kestrel")
        # The local tool's published schema is unchanged: these are the account's.
        self.assertNotIn("possibly_stale", json.dumps(server.FindResult.model_json_schema()))

    def test_a_write_answered_by_a_gateway_error_is_unsure_and_a_read_is_offline(self) -> None:
        """A 502 or 504 can come back after the account committed the write. "Your memories
        were not changed" was a guess, and the assistant's retry stored the fact twice."""
        write_link(self.state_dir)
        for status in (500, 502, 503, 504):
            self.account.requests.clear()
            self.account.status = status
            with self.assertRaises(hosted_proxy.HostedError) as caught:
                self.call("memorysafe_remember", {"content": "x", "category": "other"})
            self.assertEqual(str(caught.exception), hosted_proxy.UNSURE, status)
            self.assertEqual(len(self.account.requests), 1)
            with self.assertRaises(device_link.LinkError) as offline:
                self.call("memorysafe_find", {"query": "", "limit": 1})
            self.assertEqual(str(offline.exception), device_link.OFFLINE, status)
        # A refusal that is not the server failing (403, 429) changed nothing.
        self.account.status = 429
        with self.assertRaises(device_link.LinkError) as refused:
            self.call("memorysafe_remember", {"content": "x", "category": "other"})
        self.assertEqual(str(refused.exception), device_link.OFFLINE)

    def test_two_calls_after_the_expiry_refresh_once(self) -> None:
        """A refresh token is single use, and a replayed one signs every computer out. Tool
        calls run in worker threads, so two landing just after the hourly expiry both sent
        the same refresh token (10 Nov 2026)."""
        import threading

        write_link(self.state_dir, expires_in=1)
        refreshes: list[str] = []
        gate = threading.Barrier(2)

        def slow_refresh(url: str, form: dict | None, headers: dict) -> tuple[int, dict]:
            refreshes.append(str((form or {}).get("refresh_token")))
            time.sleep(0.2)  # long enough for the other thread to arrive
            return 200, {"access_token": "access-2", "token_type": "Bearer", "expires_in": 3600,
                         "refresh_token": "refresh-2", "scope": "memories offline_access"}

        tokens: list[str] = []

        def one() -> None:
            gate.wait()
            tokens.append(device_link.access_token(self.state_dir, http=slow_refresh))

        threads = [threading.Thread(target=one) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(refreshes, ["refresh-1"])
        self.assertEqual(tokens, ["access-2", "access-2"])
        self.assertEqual(device_link.read_link(self.state_dir)["refresh_token"], "refresh-2")

    def test_a_read_that_timed_out_changed_nothing_so_it_is_offline(self) -> None:
        write_link(self.state_dir)
        self.account.raise_error = TimeoutError("timed out")
        for name in sorted(hosted_proxy.READ_ONLY_TOOLS):
            with self.assertRaises(device_link.LinkError) as caught:
                self.call(name, {})
            self.assertEqual(str(caught.exception), device_link.OFFLINE, name)

    def test_the_accounts_own_refusal_passes_in_its_words_without_the_protocol_prefix(self) -> None:
        write_link(self.state_dir)
        self.account.tool_error = "This project is read-only for you."
        with self.assertRaises(hosted_proxy.HostedError) as caught:
            self.call("memorysafe_remember", {"content": "x", "category": "other"})
        self.assertEqual(str(caught.exception), "This project is read-only for you.")

    def test_a_protocol_error_is_the_servers_message(self) -> None:
        write_link(self.state_dir)
        self.account.answer = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Unknown tool: nope"}}).encode()
        with self.assertRaises(hosted_proxy.HostedError) as caught:
            self.call("nope", {})
        self.assertEqual(str(caught.exception), "Unknown tool: nope")

    def test_an_answer_this_plugin_cannot_read_says_so(self) -> None:
        write_link(self.state_dir)
        for answer in (b"<html>", b"[]", b'{"result": 3}', b'{"result": {"content": [{"type": "text", "text": "hi"}]}}'):
            self.account.answer = answer
            with self.assertRaises(hosted_proxy.HostedError) as caught:
                self.call("memorysafe_find", {"query": "", "limit": 1})
            self.assertEqual(str(caught.exception), hosted_proxy.UNREADABLE, answer)

    def test_a_text_only_result_is_read_as_json(self) -> None:
        write_link(self.state_dir)
        self.account.answer = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"query": "", "count": 0, "memories": []})}]}}).encode()
        self.assertEqual(self.call("memorysafe_find", {"query": "", "limit": 1})["count"], 0)

    def test_plain_http_is_never_used_for_a_token(self) -> None:
        with self.assertRaises(OSError):
            hosted_proxy._post("http://memory.test/mcp", b"{}", {})


class ServerToolTests(ProxyBase):
    """The local tool functions, in local mode, in hosted mode, and in the hosted edition."""

    def setUp(self) -> None:
        super().setUp()
        root = Path(self.temporary.name)
        self.local_db = root / "local" / "memorysafe.sqlite3"
        self.local_db.parent.mkdir()
        env = patch.dict("os.environ", {"MEMORYSAFE_STATE_DIR": str(self.state_dir), "MEMORYSAFE_DB_PATH": str(self.local_db)})
        env.start()
        self.addCleanup(env.stop)
        post_patch = patch.object(hosted_proxy, "_post", self.account)
        post_patch.start()
        self.addCleanup(post_patch.stop)
        self.addCleanup(server._stores.clear)
        self.addCleanup(server.use_store_provider, None)

    def hosted(self) -> None:
        storage_mode.write_storage_mode(self.state_dir, storage_mode.HOSTED)
        write_link(self.state_dir)

    def test_local_mode_uses_the_local_file_and_sends_nothing(self) -> None:
        stored = server.remember("The team prefers plain answers", "preference")
        self.assertIn(stored.decision, ("STORE", "PROTECT"))
        self.assertEqual(server.find_memories("plain answers", 5).count, 1)
        self.assertEqual(self.account.requests, [])
        self.assertEqual(len(MemoryStore(self.local_db).find("", 10)), 1)
        self.assertEqual(len(self.account_store.find("", 10)), 0)

    def test_hosted_mode_forwards_every_memory_tool_by_name_and_leaves_the_local_file_alone(self) -> None:
        self.hosted()
        stored = server.remember("The team prefers plain answers", "preference")
        self.assertIn(stored.decision, ("STORE", "PROTECT"))
        found = server.find_memories("plain answers", 5)
        self.assertEqual(found.count, 1)
        memory_id = found.memories[0].memory_id
        server.protect_memory(memory_id, True)
        self.assertTrue(server.explain_memory(memory_id).protected)
        server.review_conflicts(False)
        server.forget_memory(memory_id, True)
        server.restore_memory(memory_id, True)
        server.set_automatic_mode(True)
        batch = server.automatic_capture(["The release is on Friday", "Reviews need two approvals"], "project")
        self.assertEqual(batch.saved, 2)
        names = [message["params"]["name"] for _url, message, _headers in self.account.requests]
        self.assertEqual(names, [
            "memorysafe_remember", "memorysafe_find", "memorysafe_protect", "memorysafe_explain",
            "memorysafe_review_conflicts", "memorysafe_forget", "memorysafe_restore", "memorysafe_set_auto_mode",
            "memorysafe_auto_capture",
        ])
        capture = self.account.requests[-1][1]["params"]["arguments"]
        self.assertEqual(capture, {"facts": ["The release is on Friday", "Reviews need two approvals"], "category": "project"})
        self.assertFalse(self.local_db.exists(), "hosted mode must not even create the local file")
        self.assertEqual(len(self.account_store.find("", 10)), 3)

    def test_a_resolve_conflict_forwards_its_three_arguments(self) -> None:
        self.hosted()
        self.account.tool_error = "Conflict 7 was not found."
        with self.assertRaises(hosted_proxy.HostedError):
            server.resolve_conflict(7, "supersede", True)
        self.assertEqual(self.account.requests[0][1]["params"]["arguments"], {"conflict_id": 7, "action": "supersede", "confirm": True})

    def test_hosted_health_is_the_accounts_numbers_with_this_computers_dashboard_address(self) -> None:
        self.hosted()
        self.account_store.remember("The team prefers plain answers", "preference")
        health = server.memory_health(False)
        self.assertEqual(health.active_memories, 1)
        self.assertEqual(health.database_path, "Stored in your MemorySafe account")
        self.assertEqual(health.live_token_metrics.kind, "hosted_estimate")
        self.assertEqual(health.live_token_metrics.counted_memories, 1)
        self.assertTrue(health.dashboard_url.startswith("http://127.0.0.1:"))
        self.assertIsNone(health.opened)
        self.assertEqual(len(health.recent_memories), 1)
        self.assertEqual(self.account.requests[0][1]["params"], {"name": "memorysafe_health", "arguments": {}})

    def test_the_failure_sentences_reach_the_tool_caller_unchanged(self) -> None:
        storage_mode.write_storage_mode(self.state_dir, storage_mode.HOSTED)
        with self.assertRaises(device_link.LinkError) as caught:
            server.find_memories("", 5)
        self.assertEqual(str(caught.exception), device_link.NOT_LINKED)
        write_link(self.state_dir)
        self.account.raise_error = ConnectionRefusedError()
        with self.assertRaises(device_link.LinkError) as caught:
            server.remember("x", "other")
        self.assertEqual(str(caught.exception), device_link.OFFLINE)

    def test_switching_back_to_local_takes_effect_on_the_next_call(self) -> None:
        self.hosted()
        server.remember("In the account", "other")
        storage_mode.write_storage_mode(self.state_dir, storage_mode.LOCAL)
        server.remember("On this computer", "other")
        self.assertEqual(len(self.account.requests), 1)
        self.assertEqual(len(MemoryStore(self.local_db).find("", 10)), 1)

    def test_the_hosted_edition_itself_never_forwards(self) -> None:
        # A deployment with a store provider is the account. Even with a hosted-mode file
        # on its disk, its tools answer from the provider and send nothing anywhere.
        self.hosted()
        provided = MemoryStore(Path(self.temporary.name) / "provided.sqlite3")
        server.use_store_provider(lambda: provided)
        self.assertFalse(server._hosted_mode())
        server.remember("Served by the provider", "other")
        self.assertEqual(self.account.requests, [])
        self.assertEqual(len(provided.find("", 10)), 1)

    def test_the_doctor_stays_local_in_hosted_mode(self) -> None:
        self.hosted()
        report = server.doctor()
        self.assertIn(report.overall_status, {"healthy", "degraded", "broken", "warning", "error"})
        self.assertEqual(self.account.requests, [])


class MeasureTests(ProxyBase):
    def test_measure_reports_medians_against_the_stop_line(self) -> None:
        write_link(self.state_dir)
        ticks = iter(range(0, 10_000))
        clock = lambda: next(ticks) * 0.010  # noqa: E731 - every timed action takes one tick (10 ms)
        report = hosted_proxy.measure(lambda: None, samples=5, state_dir=self.state_dir, post=self.account, clock=clock)
        self.assertEqual(report["samples"], 5)
        self.assertEqual(report["stop_line_ms"], hosted_proxy.STOP_LINE_MS)
        self.assertEqual(report["local_median_ms"], 10.0)
        self.assertEqual(report["hosted_median_ms"], 10.0)
        self.assertEqual(report["added_median_ms"], 0.0)
        self.assertTrue(report["within_stop_line"])
        calls = [message["params"] for _url, message, _headers in self.account.requests]
        self.assertEqual(calls, [{"name": "memorysafe_review_conflicts", "arguments": {"include_resolved": False}}] * 5)
        # A find is a use: twenty of them moved a memory up what every session carries.
        self.assertEqual(self.account.store.health()["recall"]["times_memory_was_consulted"], 0)

    def test_the_cli_refuses_to_measure_in_local_mode(self) -> None:
        from memorysafe_chatgpt import cli

        with patch.dict("os.environ", {"MEMORYSAFE_STATE_DIR": str(self.state_dir)}), \
                patch("sys.argv", ["memorysafe", "hosted-latency"]), redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("local mode", err.getvalue())
        self.assertEqual(self.account.requests, [])

    def test_the_cli_prints_the_verdict_in_hosted_mode(self) -> None:
        from memorysafe_chatgpt import cli

        storage_mode.write_storage_mode(self.state_dir, storage_mode.HOSTED)
        write_link(self.state_dir)
        local_db = Path(self.temporary.name) / "cli-local.sqlite3"
        with patch.dict("os.environ", {"MEMORYSAFE_STATE_DIR": str(self.state_dir), "MEMORYSAFE_DB_PATH": str(local_db)}), \
                patch.object(hosted_proxy, "_post", self.account), \
                patch("sys.argv", ["memorysafe", "hosted-latency", "--samples", "3", "--json"]), \
                redirect_stdout(io.StringIO()) as out:
            cli.main()
        report = json.loads(out.getvalue())
        self.assertEqual(report["samples"], 3)
        self.assertIn("within_stop_line", report)
        self.assertEqual(len(self.account.requests), 3)


class ExportEndpointTests(unittest.TestCase):
    """/api/export refuses in hosted mode, as /api/import does; the page hides the panel."""

    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from memorysafe_chatgpt import setup_app as setup_app_module
        from memorysafe_chatgpt.setup_app import SetupPaths

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = SetupPaths(
            install_root=root, state_dir=root / "runtime-state", database_path=root / "data" / "memorysafe.sqlite3",
            secret_file=root / ".secrets" / "key", tunnel_id_file=root / "tunnel-id",
            health_url_file=root / "tunnel.url", legal_dir=root / "legal", connect_url="https://example.invalid",
        )
        self.paths.state_dir.mkdir()
        paths_patch = patch.object(setup_app_module.SetupHandler, "paths", self.paths)
        paths_patch.start()
        self.addCleanup(paths_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app_module.SetupHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.token = setup_app_module.SetupHandler.setup_token

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", path, body=json.dumps(body),
                           headers={"Content-Type": "application/json", "X-MemorySafe-Setup-Token": self.token})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw or b"{}")

    def test_export_refuses_in_hosted_mode_and_writes_no_file(self) -> None:
        MemoryStore(self.paths.database_path).remember("The team prefers plain answers", "preference")
        storage_mode.write_storage_mode(self.paths.state_dir, storage_mode.HOSTED)
        status, payload = self._post("/api/export", {})
        self.assertEqual(status, 409, payload)
        self.assertIn("hosted account", payload["error"])
        self.assertIn("On this computer", payload["error"])
        self.assertEqual([p for p in self.paths.state_dir.rglob("*") if p.is_file() and p.suffix == ".json" and "export" in p.name], [])

    def test_export_works_in_local_mode(self) -> None:
        MemoryStore(self.paths.database_path).remember("The team prefers plain answers", "preference")
        status, payload = self._post("/api/export", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 1)

    def test_the_dashboard_hides_the_move_panel_in_hosted_mode(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        self.assertIn('byId("hosted-panel").hidden = hostedMode;', dashboard_html())


if __name__ == "__main__":
    unittest.main()
