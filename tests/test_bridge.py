#!/usr/bin/env python3
"""Tests for the Telegram bridge.

Run: python3 -m unittest discover -s tests -v  (from the bridge directory)

The Telegram and OpenHands sides are exercised against **real local HTTP servers**
on loopback ports, so the request shapes, the auth headers, the redaction and the
chunking are checked over a socket rather than against a mock that could agree with
a bug. No real bot token and no real OpenHands deployment are involved.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import (  # noqa: E402
    CloudBackend,
    Config,
    LocalAgentServerBackend,
    State,
    Telegram,
    chunk_message,
    handle_command,
    is_authorized,
    load_config,
    process_update,
    redact,
    route_message,
)


class RecordingHandler(BaseHTTPRequestHandler):
    """Records requests and replies from a per-test script."""

    server_version = "Recording/1.0"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        entry = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }
        self.server.requests.append(entry)  # type: ignore[attr-defined]
        responder = self.server.responders.get(self.path)  # type: ignore[attr-defined]
        if responder is None:
            status, payload = 200, {"ok": True}
        else:
            status, payload = responder(entry)
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, *args) -> None:  # silence
        return


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.responders = {}  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def header(headers: dict[str, str], name: str) -> str | None:
        """HTTP header lookup is case-insensitive; compare accordingly."""
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return value
        return None


class TestConfig(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "TELEGRAM_BOT_TOKEN": "123456:secret-token-value",
            "TELEGRAM_ALLOWED_USER_IDS": "42, 99",
            "OPENHANDS_MODE": "cloud",
            "OPENHANDS_CLOUD_API_KEY": "cloud-key-value",
        }

    def test_loads_allowlist(self) -> None:
        config = load_config(self.base_env())
        self.assertEqual(config.allowed_user_ids, frozenset({42, 99}))

    def test_refuses_to_start_without_allowlist(self) -> None:
        env = self.base_env()
        env["TELEGRAM_ALLOWED_USER_IDS"] = ""
        with self.assertRaises(ValueError):
            load_config(env).require_safe()

    def test_refuses_cloud_without_key(self) -> None:
        env = self.base_env()
        del env["OPENHANDS_CLOUD_API_KEY"]
        with self.assertRaises(ValueError):
            load_config(env).require_safe()

    def test_refuses_local_without_session_key(self) -> None:
        env = self.base_env()
        env["OPENHANDS_MODE"] = "local"
        with self.assertRaises(ValueError):
            load_config(env).require_safe()

    def test_rejects_unknown_mode(self) -> None:
        env = self.base_env()
        env["OPENHANDS_MODE"] = "sneaky"
        with self.assertRaises(ValueError):
            load_config(env).require_safe()


class TestAuthorization(unittest.TestCase):
    def test_only_integer_allowed_ids_pass(self) -> None:
        allowed = frozenset({42})
        self.assertTrue(is_authorized(42, allowed))
        self.assertFalse(is_authorized(43, allowed))
        self.assertFalse(is_authorized("42", allowed))
        self.assertFalse(is_authorized(None, allowed))
        self.assertFalse(is_authorized(True, allowed))


class TestRedaction(unittest.TestCase):
    def test_removes_known_secrets(self) -> None:
        text = "failed with token 123456:secret-token-value while calling"
        out = redact(text, "123456:secret-token-value")
        self.assertNotIn("secret-token-value", out)
        self.assertIn("***REDACTED***", out)

    def test_ignores_short_values(self) -> None:
        self.assertEqual(redact("abc", "abc"), "abc")


class TestChunking(unittest.TestCase):
    def test_short_message_is_untouched(self) -> None:
        self.assertEqual(chunk_message("hello"), ["hello"])

    def test_splits_on_newline_and_keeps_everything(self) -> None:
        text = "a" * 3000 + "\n" + "b" * 3000
        chunks = chunk_message(text, limit=4096)
        self.assertEqual(len(chunks), 2)
        # The newline rides with the first chunk, so nothing is lost.
        self.assertEqual(chunks[0], "a" * 3000 + "\n")
        self.assertEqual("".join(chunks), text)

    def test_hard_split_when_no_newline(self) -> None:
        chunks = chunk_message("x" * 9000, limit=4096)
        self.assertEqual([len(c) for c in chunks], [4096, 4096, 808])


class TestTelegramAgainstRealServer(ServerCase):
    def test_get_updates_shape_and_offset(self) -> None:
        self.server.responders["/bot123456:secret-token-value/getUpdates"] = lambda e: (
            200, {"ok": True, "result": [{"update_id": 7}]}
        )
        telegram = Telegram("123456:secret-token-value", api_base=self.base())
        updates = telegram.get_updates(offset=5)
        sent = self.server.requests[-1]
        self.assertIn("/bot123456:secret-token-value/getUpdates", sent["path"])
        self.assertEqual(sent["body"]["offset"], 5)
        self.assertEqual(updates[0]["update_id"], 7)

    def test_send_message_chunks_over_the_wire(self) -> None:
        self.server.responders["/bot123456:secret-token-value/sendMessage"] = lambda e: (
            200, {"ok": True}
        )
        telegram = Telegram("123456:secret-token-value", api_base=self.base())
        telegram.send_message(42, "y" * 5000)
        sent = [r for r in self.server.requests if "sendMessage" in r["path"]]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["body"]["chat_id"], 42)


class TestCloudBackendAgainstRealServer(ServerCase):
    def config(self) -> Config:
        return Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="cloud",
            state_file="/tmp/unused",
            cloud_api_key="cloud-key-value",
            cloud_base_url=self.base(),
            cloud_repository="owner/repo",
            cloud_branch="main",
        )

    def test_start_returns_id_directly(self) -> None:
        self.server.responders["/api/v1/app-conversations"] = lambda e: (
            200, {"app_conversation_id": "conv-1"}
        )
        backend = CloudBackend(self.config())
        self.assertEqual(backend.start("hi"), "conv-1")
        sent = self.server.requests[-1]
        self.assertEqual(self.header(sent["headers"], "Authorization"), "Bearer cloud-key-value")
        self.assertEqual(sent["body"]["selected_repository"], "owner/repo")

    def test_send_uses_bearer_auth(self) -> None:
        self.server.responders["/api/v1/app-conversations/conv-1/send-message"] = lambda e: (
            200, {"success": True}
        )
        CloudBackend(self.config()).send("conv-1", "follow up")
        sent = self.server.requests[-1]
        self.assertEqual(self.header(sent["headers"], "Authorization"), "Bearer cloud-key-value")
        self.assertTrue(sent["body"]["run"])

    def test_status_reads_execution_state(self) -> None:
        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = lambda e: (
            200, {"items": [{"sandbox_status": "RUNNING", "execution_status": "idle"}]}
        )
        self.assertEqual(CloudBackend(self.config()).status("conv-1"), "RUNNING / idle")


class TestLocalBackendAgainstRealServer(ServerCase):
    def config(self) -> Config:
        return Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="local",
            state_file="/tmp/unused",
            local_agent_server_url=self.base(),
            local_session_api_key="session-key-value",
            local_working_dir="/workspace",
            local_agent_config={"kind": "Agent", "llm": {"model": "m", "api_key": "*"}},
        )

    def test_start_uses_session_header(self) -> None:
        self.server.responders["/api/conversations"] = lambda e: (200, {"id": "local-1"})
        backend = LocalAgentServerBackend(self.config())
        self.assertEqual(backend.start("hi"), "local-1")
        sent = self.server.requests[-1]
        self.assertEqual(self.header(sent["headers"], "X-Session-API-Key"), "session-key-value")
        self.assertEqual(sent["body"]["workspace"]["working_dir"], "/workspace")

    def test_send_posts_an_event(self) -> None:
        self.server.responders["/api/conversations/local-1/events"] = lambda e: (200, {"success": True})
        LocalAgentServerBackend(self.config()).send("local-1", "more")
        sent = self.server.requests[-1]
        self.assertEqual(sent["body"]["role"], "user")
        self.assertTrue(sent["body"]["run"])

    def test_start_without_agent_config_is_an_error(self) -> None:
        config = self.config()
        config.local_agent_config = None
        with self.assertRaises(RuntimeError):
            LocalAgentServerBackend(config).start("hi")


class FakeBackend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def start(self, text: str) -> str:
        self.started.append(text)
        return f"conv-{len(self.started)}"

    def send(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))

    def status(self, conversation_id: str) -> str:
        return "RUNNING"


class TestRouting(unittest.TestCase):
    def make(self) -> tuple[State, FakeBackend]:
        return State(), FakeBackend()

    def test_first_message_starts_a_conversation(self) -> None:
        state, backend = self.make()
        route_message("hello", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertEqual(backend.started, ["hello"])
        self.assertEqual(state.conversations["1:7"], "conv-1")

    def test_second_message_reuses_the_conversation(self) -> None:
        state, backend = self.make()
        route_message("hello", chat_id=1, user_id=7, state=state, backend=backend)
        route_message("again", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertEqual(len(backend.started), 1)
        self.assertEqual(backend.sent, [("conv-1", "again")])

    def test_conversations_are_per_user(self) -> None:
        state, backend = self.make()
        route_message("a", chat_id=1, user_id=7, state=state, backend=backend)
        route_message("b", chat_id=1, user_id=8, state=state, backend=backend)
        self.assertEqual(len(backend.started), 2)

    def test_help_and_id_are_commands(self) -> None:
        state, backend = self.make()
        reply = handle_command("/id", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertIsNotNone(reply)
        self.assertIn("7", reply.text)
        reply = handle_command("/help", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertIn("/status", reply.text)
        self.assertEqual(backend.started, [])

    def test_status_without_conversation(self) -> None:
        state, backend = self.make()
        reply = handle_command("/status", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertIn("No conversation", reply.text)

    def test_new_alone_clears_the_conversation(self) -> None:
        state, backend = self.make()
        state.conversations["1:7"] = "conv-old"
        reply = handle_command("/new", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertIn("Cleared", reply.text)
        self.assertNotIn("1:7", state.conversations)
        self.assertEqual(backend.started, [])

    def test_new_with_text_starts_immediately(self) -> None:
        state, backend = self.make()
        reply = handle_command("/new hello agent", chat_id=1, user_id=7, state=state, backend=backend)
        self.assertIn("Started", reply.text)
        self.assertEqual(backend.started, ["hello agent"])


class TestUpdateAuthorization(unittest.TestCase):
    def config(self) -> Config:
        return Config(
            telegram_token="t",
            allowed_user_ids=frozenset({7}),
            mode="local",
            state_file="/tmp/unused",
        )

    def test_stranger_is_refused_before_any_backend_call(self) -> None:
        state, backend = State(), FakeBackend()
        update = {"update_id": 1, "message": {"from": {"id": 999}, "chat": {"id": 1}, "text": "hi"}}
        reply = process_update(update, state=state, config=self.config(), backend=backend)
        self.assertEqual(reply, "Not authorized.")
        self.assertEqual(backend.started, [])

    def test_authorized_user_is_served(self) -> None:
        state, backend = State(), FakeBackend()
        update = {"update_id": 1, "message": {"from": {"id": 7}, "chat": {"id": 1}, "text": "hi"}}
        reply = process_update(update, state=state, config=self.config(), backend=backend)
        self.assertIn("Sent to the agent", reply)
        self.assertEqual(backend.started, ["hi"])

    def test_update_without_sender_is_not_authorized(self) -> None:
        state, backend = State(), FakeBackend()
        update = {"update_id": 1, "message": {"chat": {"id": 1}, "text": "hi"}}
        reply = process_update(update, state=state, config=self.config(), backend=backend)
        self.assertEqual(reply, "Not authorized.")


class TestPreflightAgainstRealServer(ServerCase):
    def config(self, mode: str = "cloud") -> Config:
        return Config(
            telegram_token="tok",
            allowed_user_ids=frozenset({1}),
            mode=mode,
            state_file="/tmp/unused",
            cloud_api_key="cloud-key-value",
            cloud_base_url=self.base(),
        )

    def test_reports_telegram_username_without_leaking_the_token(self) -> None:
        self.server.responders["/bottok/getMe"] = lambda e: (
            200, {"ok": True, "result": {"username": "horizon_bot"}}
        )
        import bridge as bridge_module

        original = bridge_module.TELEGRAM_API
        bridge_module.TELEGRAM_API = self.base()
        try:
            ok, message = bridge_module.verify_telegram_token(self.config())
        finally:
            bridge_module.TELEGRAM_API = original
        self.assertTrue(ok)
        self.assertIn("horizon_bot", message)

    def test_rejects_a_bad_telegram_token(self) -> None:
        self.server.responders["/bottok/getMe"] = lambda e: (
            404, {"ok": False, "error_code": 404, "description": "Not Found"}
        )
        import bridge as bridge_module

        original = bridge_module.TELEGRAM_API
        bridge_module.TELEGRAM_API = self.base()
        try:
            ok, message = bridge_module.verify_telegram_token(self.config())
        finally:
            bridge_module.TELEGRAM_API = original
        self.assertFalse(ok)
        self.assertIn("404", message)

    def test_preflight_fails_when_both_are_wrong(self) -> None:
        self.server.responders["/bottok/getMe"] = lambda e: (
            404, {"ok": False, "error_code": 404, "description": "Not Found"}
        )
        self.server.responders["/api/v1/users/me"] = lambda e: (401, {"detail": "nope"})
        import bridge as bridge_module

        original = bridge_module.TELEGRAM_API
        bridge_module.TELEGRAM_API = self.base()
        try:
            code = bridge_module.preflight(self.config())
        finally:
            bridge_module.TELEGRAM_API = original
        self.assertEqual(code, 1)

    def test_preflight_passes_when_both_are_ok(self) -> None:
        self.server.responders["/bottok/getMe"] = lambda e: (
            200, {"ok": True, "result": {"username": "horizon_bot"}}
        )
        self.server.responders["/api/v1/users/me"] = lambda e: (200, {"id": "u1"})
        import bridge as bridge_module

        original = bridge_module.TELEGRAM_API
        bridge_module.TELEGRAM_API = self.base()
        try:
            code = bridge_module.preflight(self.config())
        finally:
            bridge_module.TELEGRAM_API = original
        self.assertEqual(code, 0)


class TestAgentReplyExtraction(unittest.TestCase):
    """Shapes copied from a real event fetched from the live API."""

    def test_reads_a_real_agent_message(self) -> None:
        import bridge as m

        # Trimmed from GET /api/v1/conversation/{id}/events/search.
        event = {
            "id": "edc83da5-eaf0-415d-b2b6-808b96483666",
            "timestamp": "2026-09-27T00:25:13.812117",
            "source": "agent",
            "kind": "MessageEvent",
            "llm_message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hallo — womit soll ich anfangen?"}],
                "reasoning_content": "the user is frustrated",
            },
        }
        self.assertEqual(m.extract_agent_text(event), "Hallo — womit soll ich anfangen?")

    def test_never_leaks_the_models_private_reasoning(self) -> None:
        import bridge as m

        event = {
            "source": "agent",
            "kind": "MessageEvent",
            "llm_message": {"role": "assistant", "content": [], "reasoning_content": "secret plan"},
        }
        self.assertIsNone(m.extract_agent_text(event))

    def test_ignores_user_and_tool_events(self) -> None:
        import bridge as m

        self.assertIsNone(m.extract_agent_text({"kind": "MessageEvent", "source": "user",
                                                "llm_message": {"content": [{"text": "hi"}]}}))
        self.assertIsNone(m.extract_agent_text({"kind": "ActionEvent", "source": "agent",
                                                "tool_name": "terminal"}))
        self.assertIsNone(m.extract_agent_text({"kind": "ObservationEvent", "source": "environment"}))
        self.assertIsNone(m.extract_agent_text({"kind": "ConversationStateUpdateEvent",
                                                "source": "environment"}))

    def test_ignores_a_non_assistant_role(self) -> None:
        import bridge as m

        event = {"kind": "MessageEvent", "source": "agent",
                 "llm_message": {"role": "user", "content": [{"text": "echo"}]}}
        self.assertIsNone(m.extract_agent_text(event))

    def test_handles_a_string_content(self) -> None:
        import bridge as m

        event = {"kind": "MessageEvent", "source": "agent", "llm_message": {"content": "plain"}}
        self.assertEqual(m.extract_agent_text(event), "plain")

    def test_handles_legacy_assistant_shape(self) -> None:
        import bridge as m

        event = {"kind": "MessageEvent", "source": "assistant", "message": {"content": "old"}}
        self.assertEqual(m.extract_agent_text(event), "old")

    def test_empty_agent_message_is_ignored(self) -> None:
        import bridge as m

        event = {"kind": "MessageEvent", "source": "agent", "llm_message": {"content": []}}
        self.assertIsNone(m.extract_agent_text(event))

    def test_collect_drops_already_seen_and_keeps_order(self) -> None:
        import bridge as m

        def msg(eid, ts, text, source="agent"):
            return {"id": eid, "timestamp": ts, "kind": "MessageEvent", "source": source,
                    "llm_message": {"role": "assistant", "content": [{"text": text}]}}

        events = [msg("b", "2", "second"), msg("a", "1", "first"), msg("a", "1", "first")]
        seen: set[str] = set()
        self.assertEqual(m.collect_new_agent_messages(events, seen), ["first", "second"])
        # A second pass yields nothing: the ids are now known.
        self.assertEqual(m.collect_new_agent_messages(events, seen), [])


class TestChatIdFromKey(unittest.TestCase):
    def test_recovers_the_chat(self) -> None:
        import bridge as m

        self.assertEqual(m.chat_id_from_key("2065255514:2065255514"), 2065255514)

    def test_rejects_a_malformed_key(self) -> None:
        import bridge as m

        self.assertIsNone(m.chat_id_from_key("not-a-chat"))


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class TestRelayAgainstRealServer(ServerCase):
    def test_relay_fetches_and_sends_new_agent_messages(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/conversation/conv-1/events/search?limit=50&sort_order=TIMESTAMP_DESC"] = lambda e: (
            200,
            {
                "items": [
                    {"id": "e1", "timestamp": "1", "kind": "MessageEvent", "source": "agent",
                     "llm_message": {"role": "assistant", "content": [{"text": "Hello from the agent"}]}},
                    {"id": "u1", "timestamp": "0", "kind": "MessageEvent", "source": "user",
                     "llm_message": {"role": "user", "content": [{"text": "hi"}]}},
                ]
            },
        )
        config = Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="cloud",
            state_file="/tmp/unused",
            cloud_api_key="cloud-key-value",
            cloud_base_url=self.base(),
        )
        backend = m.CloudBackend(config)
        telegram = FakeTelegram()
        state = m.State(conversations={"2065255514:2065255514": "conv-1"})

        sent = m.relay_agent_replies(state=state, backend=backend, telegram=telegram)

        self.assertEqual(sent, 1)
        self.assertEqual(telegram.sent, [(2065255514, "Hello from the agent")])
        self.assertEqual(state.seen_events["conv-1"], ["e1"])

        # Running again sends nothing, so the user is not spammed.
        self.assertEqual(m.relay_agent_replies(state=state, backend=backend, telegram=telegram), 0)

    def test_relay_survives_a_backend_failure(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/conversation/conv-1/events/search?limit=50&sort_order=TIMESTAMP_DESC"] = lambda e: (
            500, {"detail": "boom"}
        )
        config = Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="cloud",
            state_file="/tmp/unused",
            cloud_api_key="k",
            cloud_base_url=self.base(),
        )
        state = m.State(conversations={"1:1": "conv-1"})
        telegram = FakeTelegram()
        self.assertEqual(
            m.relay_agent_replies(state=state, backend=m.CloudBackend(config), telegram=telegram), 0
        )
        self.assertEqual(telegram.sent, [])


class TestRecordNormalization(unittest.TestCase):
    """The API returns a bare list in some deployments; that crashed the bridge."""

    def test_a_bare_list_is_accepted(self) -> None:
        import bridge as m

        payload = [{"app_conversation_id": "conv-1"}]
        self.assertEqual(m.records_of(payload), payload)
        self.assertEqual(m.first_record(payload)["app_conversation_id"], "conv-1")

    def test_items_wrapper_is_accepted(self) -> None:
        import bridge as m

        payload = {"items": [{"app_conversation_id": "conv-2"}]}
        self.assertEqual(m.records_of(payload), [{"app_conversation_id": "conv-2"}])

    def test_a_single_object_is_accepted(self) -> None:
        import bridge as m

        self.assertEqual(m.records_of({"id": "x"}), [{"id": "x"}])

    def test_empty_and_none_are_safe(self) -> None:
        import bridge as m

        self.assertEqual(m.records_of(None), [])
        self.assertEqual(m.records_of([]), [])
        self.assertEqual(m.records_of({"items": []}), [])
        self.assertEqual(m.first_record(None), {})

    def test_non_dict_entries_are_dropped(self) -> None:
        import bridge as m

        self.assertEqual(m.records_of(["nope", {"id": "ok"}]), [{"id": "ok"}])


class TestCloudStartAgainstRealServer(ServerCase):
    def config(self) -> Config:
        return Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="cloud",
            state_file="/tmp/unused",
            cloud_api_key="k",
            cloud_base_url=self.base(),
        )

    def test_start_accepts_a_bare_list_response(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/app-conversations"] = lambda e: (
            200, [{"app_conversation_id": "conv-list"}]
        )
        self.assertEqual(m.CloudBackend(self.config()).start("hello"), "conv-list")

    def test_start_accepts_a_start_task_and_polls_it(self) -> None:
        import bridge as m

        def create(e):
            return 200, [{"id": "task-1"}]

        def poll(e):
            return 200, {"items": [{"app_conversation_id": "conv-polled"}]}

        self.server.responders["/api/v1/app-conversations"] = create
        self.server.responders["/api/v1/app-conversations/start-tasks?ids=task-1"] = poll
        self.assertEqual(m.CloudBackend(self.config()).start("hello"), "conv-polled")

    def test_status_reads_a_bare_list(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = lambda e: (
            200, [{"sandbox_status": "RUNNING", "execution_status": "running"}]
        )
        self.assertEqual(m.CloudBackend(self.config()).status("conv-1"), "RUNNING / running")


class TestCloudSendAgainstRealServer(ServerCase):
    """send-message returns 409 while a sandbox is paused, and the message is lost."""

    def config(self) -> Config:
        return Config(
            telegram_token="t",
            allowed_user_ids=frozenset({1}),
            mode="cloud",
            state_file="/tmp/unused",
            cloud_api_key="k",
            cloud_base_url=self.base(),
        )

    def _running(self, e):
        return 200, [{"sandbox_status": "RUNNING", "sandbox_id": "SB1"}]

    def test_sends_the_documented_payload(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = self._running
        self.server.responders["/api/v1/app-conversations/conv-1/send-message"] = lambda e: (200, {})
        m.CloudBackend(self.config()).send("conv-1", "hello agent")

        sends = [r for r in self.server.requests if r["path"].endswith("/send-message")]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["body"], {"content": [{"type": "text", "text": "hello agent"}], "run": True})
        self.assertEqual(self.header(sends[0]["headers"], "Authorization"), "Bearer k")

    def test_resumes_a_paused_sandbox_before_sending(self) -> None:
        import bridge as m

        # PAUSED on the first lookup, RUNNING afterwards.
        calls = {"n": 0}

        def record(e):
            calls["n"] += 1
            state = "PAUSED" if calls["n"] == 1 else "RUNNING"
            return 200, [{"sandbox_status": state, "sandbox_id": "SB1"}]

        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = record
        self.server.responders["/api/v1/sandboxes/SB1/resume"] = lambda e: (200, {})
        self.server.responders["/api/v1/app-conversations/conv-1/send-message"] = lambda e: (200, {})
        m.CloudBackend(self.config()).send("conv-1", "hi")

        resumed = [r for r in self.server.requests if "resume" in r["path"]]
        self.assertEqual(len(resumed), 1, "a paused sandbox must be resumed")
        self.assertTrue(any(r["path"].endswith("/send-message") for r in self.server.requests))

    def test_retries_once_on_409(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = self._running
        attempts = {"n": 0}

        def send(e):
            attempts["n"] += 1
            return (409, {"detail": "not running"}) if attempts["n"] == 1 else (200, {})

        self.server.responders["/api/v1/app-conversations/conv-1/send-message"] = send
        m.CloudBackend(self.config()).send("conv-1", "hi")
        self.assertEqual(attempts["n"], 2)

    def test_a_real_failure_is_raised_not_swallowed(self) -> None:
        import bridge as m

        self.server.responders["/api/v1/app-conversations?ids=conv-1"] = self._running
        self.server.responders["/api/v1/app-conversations/conv-1/send-message"] = lambda e: (500, {"detail": "boom"})
        with self.assertRaises(m.HttpError) as caught:
            m.CloudBackend(self.config()).send("conv-1", "hi")
        self.assertEqual(caught.exception.status, 500)


class TestStatePersistence(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = State(offset=5, conversations={"1:7": "conv-1"}, seen_events={"conv-1": ["e1"]})
            state.save(path)
            loaded = State.load(path)
            self.assertEqual(loaded.offset, 5)
            self.assertEqual(loaded.conversations, {"1:7": "conv-1"})
            self.assertEqual(loaded.seen_events, {"conv-1": ["e1"]})

    def test_corrupt_state_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(State.load(path).conversations, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
