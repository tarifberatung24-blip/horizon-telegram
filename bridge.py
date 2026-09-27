#!/usr/bin/env python3
"""Telegram <-> OpenHands bridge.

Lets the owner talk to an OpenHands agent from Telegram, and lets the agent run
commands on the machine this bridge runs on. Designed to sit next to the agent on
an Oracle box: it uses long polling, so no public URL, no port forwarding, no
inbound firewall rule is needed.

Security posture, in order of importance:

  * **Fail closed on the allowlist.** With no configured user ids the bridge
    refuses to start rather than answering whoever finds the bot.
  * **The bot token is a secret.** It is read from the environment, never
    logged, and redacted from any error text before that text is printed or sent.
  * **Only the configured operator is served.** Every update is checked against
    the allowlist before any backend call.
  * **Commands run as the bridge's user.** Run it as an unprivileged user; it
    does not elevate and does not need root.

Standard library only, so it installs as fast as copying one file.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
POLL_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 60


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    mode: str  # "cloud" | "local"
    state_file: str
    cloud_api_key: str | None = None
    cloud_base_url: str = "https://app.all-hands.dev"
    cloud_repository: str | None = None
    cloud_branch: str | None = None
    local_agent_server_url: str = "http://127.0.0.1:8000"
    local_session_api_key: str | None = None
    local_working_dir: str = "/workspace"
    local_agent_config: dict[str, Any] | None = None

    def require_safe(self) -> None:
        """Refuse a configuration that would serve the wrong people."""
        if not self.telegram_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not self.allowed_user_ids:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS is required and must not be empty; "
                "refusing to start so the bot does not answer strangers"
            )
        if self.mode not in ("cloud", "local"):
            raise ValueError("OPENHANDS_MODE must be 'cloud' or 'local'")
        if self.mode == "cloud" and not self.cloud_api_key:
            raise ValueError("OPENHANDS_CLOUD_API_KEY is required in cloud mode")
        if self.mode == "local" and not self.local_session_api_key:
            raise ValueError(
                "OPENHANDS_SESSION_API_KEY is required in local mode "
                "(it is in ~/.openhands/agent-canvas/api-key.txt)"
            )


def load_config(env: dict[str, str] | None = None) -> Config:
    env = env if env is not None else os.environ

    raw_ids = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
    ids: set[int] = set()
    for part in raw_ids.split(","):
        part = part.strip()
        if part:
            ids.add(int(part))

    agent_config: dict[str, Any] | None = None
    agent_config_path = env.get("OPENHANDS_AGENT_CONFIG", "").strip()
    if agent_config_path:
        with open(agent_config_path, encoding="utf-8") as handle:
            agent_config = json.load(handle)

    return Config(
        telegram_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_user_ids=frozenset(ids),
        mode=env.get("OPENHANDS_MODE", "local").strip(),
        state_file=env.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json")),
        cloud_api_key=env.get("OPENHANDS_CLOUD_API_KEY") or None,
        cloud_base_url=env.get("OPENHANDS_BASE_URL", "https://app.all-hands.dev").rstrip("/"),
        cloud_repository=env.get("OPENHANDS_REPOSITORY") or None,
        cloud_branch=env.get("OPENHANDS_BRANCH") or None,
        local_agent_server_url=env.get("LOCAL_AGENT_SERVER_URL", "http://127.0.0.1:8000").rstrip("/"),
        local_session_api_key=env.get("OPENHANDS_SESSION_API_KEY") or None,
        local_working_dir=env.get("LOCAL_WORKING_DIR", "/workspace"),
        local_agent_config=agent_config,
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def redact(text: str, *secrets: Iterable[str | None]) -> str:
    """Replace every known secret in `text` with a marker.

    Called on anything before it is logged or sent back to Telegram, so a token
    that ends up inside an exception message does not travel any further.
    """
    for secret in secrets:
        if secret and len(str(secret)) >= 8:
            text = text.replace(str(secret), "***REDACTED***")
    return text


def chunk_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split `text` into Telegram-sized chunks on newline boundaries.

    Lossless: `"".join(chunk_message(text)) == text`, so a chunked send never
    silently drops the newline it split on.
    """
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        else:
            chunks.append(remaining[: split_at + 1])
            remaining = remaining[split_at + 1 :]
    chunks.append(remaining)
    return chunks


def is_authorized(user_id: Any, allowed: frozenset[int]) -> bool:
    """True only for an integer id that is explicitly allowed.

    A missing or malformed id is never authorized, so an update without a user
    cannot slip through the check.
    """
    if not isinstance(user_id, int):
        return False
    return user_id in allowed


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def get_json(url: str, headers: dict[str, str], timeout: int = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


class Telegram:
    def __init__(self, token: str, api_base: str = TELEGRAM_API):
        self._token = token
        self._api_base = api_base.rstrip("/")

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = post_json(self._url("getUpdates"), payload, {}, timeout=timeout + 10)
        if not result.get("ok"):
            raise RuntimeError(redact(f"telegram getUpdates failed: {result}", self._token))
        return result.get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        for part in chunk_message(text):
            result = post_json(
                self._url("sendMessage"),
                {"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
                {},
            )
            if not result.get("ok"):
                raise RuntimeError(redact(f"telegram sendMessage failed: {result}", self._token))


# --------------------------------------------------------------------------- #
# OpenHands backends
# --------------------------------------------------------------------------- #


class Backend:
    """What the bridge needs from an OpenHands deployment."""

    def start(self, text: str) -> str:
        raise NotImplementedError

    def send(self, conversation_id: str, text: str) -> None:
        raise NotImplementedError

    def status(self, conversation_id: str) -> str:
        raise NotImplementedError


class CloudBackend(Backend):
    """OpenHands Cloud app-server API. Bearer auth, async start."""

    def __init__(self, config: Config):
        self._config = config

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.cloud_api_key}"}

    def start(self, text: str) -> str:
        payload: dict[str, Any] = {
            "initial_message": {"content": [{"type": "text", "text": text}]}
        }
        if self._config.cloud_repository:
            payload["selected_repository"] = self._config.cloud_repository
        if self._config.cloud_branch:
            payload["selected_branch"] = self._config.cloud_branch

        created = post_json(f"{self._config.cloud_base_url}/api/v1/app-conversations", payload, self._headers())
        if created.get("app_conversation_id"):
            return created["app_conversation_id"]

        # The start endpoint is asynchronous; poll the start task for the id.
        start_task_id = created.get("id")
        if not start_task_id:
            raise RuntimeError(redact(f"unexpected start response: {created}", self._config.cloud_api_key))
        deadline = time.time() + 180
        while time.time() < deadline:
            task = get_json(
                f"{self._config.cloud_base_url}/api/v1/app-conversations/start-tasks?ids={start_task_id}",
                self._headers(),
            )
            items = task.get("items") or ([task] if task.get("app_conversation_id") else [])
            for item in items:
                if item.get("app_conversation_id"):
                    return item["app_conversation_id"]
            time.sleep(2)
        raise RuntimeError("timed out waiting for the sandbox to start")

    def send(self, conversation_id: str, text: str) -> None:
        post_json(
            f"{self._config.cloud_base_url}/api/v1/app-conversations/{conversation_id}/send-message",
            {"content": [{"type": "text", "text": text}], "run": True},
            self._headers(),
        )

    def status(self, conversation_id: str) -> str:
        record = get_json(
            f"{self._config.cloud_base_url}/api/v1/app-conversations?ids={conversation_id}",
            self._headers(),
        )
        items = record.get("items") or [record]
        item = items[0] if items else {}
        return f"{item.get('sandbox_status', '?')} / {item.get('execution_status', '?')}"


class LocalAgentServerBackend(Backend):
    """A local agent server inside the sandbox, e.g. Agent Canvas on :8000."""

    def __init__(self, config: Config):
        self._config = config

    def _headers(self) -> dict[str, str]:
        return {"X-Session-API-Key": self._config.local_session_api_key or ""}

    def start(self, text: str) -> str:
        agent = self._config.local_agent_config
        if not agent:
            raise RuntimeError(
                "OPENHANDS_AGENT_CONFIG must point to a JSON file with the agent "
                "settings (llm + tools) when using local mode"
            )
        payload = {
            "agent": agent,
            "workspace": {"kind": "LocalWorkspace", "working_dir": self._config.local_working_dir},
            "initial_message": {"content": [{"text": text}], "run": True},
        }
        created = post_json(f"{self._config.local_agent_server_url}/api/conversations", payload, self._headers())
        conversation_id = created.get("id")
        if not conversation_id:
            raise RuntimeError(redact(f"unexpected start response: {created}", self._config.local_session_api_key))
        return conversation_id

    def send(self, conversation_id: str, text: str) -> None:
        post_json(
            f"{self._config.local_agent_server_url}/api/conversations/{conversation_id}/events",
            {"role": "user", "content": [{"text": text, "cache_prompt": False, "type": "text"}], "run": True},
            self._headers(),
        )

    def status(self, conversation_id: str) -> str:
        record = get_json(
            f"{self._config.local_agent_server_url}/api/conversations/{conversation_id}",
            self._headers(),
        )
        return str(record.get("execution_status") or record.get("status") or "?")


def build_backend(config: Config) -> Backend:
    return CloudBackend(config) if config.mode == "cloud" else LocalAgentServerBackend(config)


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


def verify_telegram_token(config: Config) -> tuple[bool, str]:
    """Ask Telegram who the bot is. Returns (ok, human message) with no secrets."""
    try:
        result = get_json(f"{TELEGRAM_API}/bot{config.telegram_token}/getMe", {})
    except Exception as exc:
        return False, f"could not reach Telegram ({redact(str(exc), config.telegram_token)})"
    if result.get("ok"):
        username = (result.get("result") or {}).get("username", "?")
        return True, f"telegram ok (@{username})"
    return False, (
        "telegram rejected the token: "
        f"{result.get('error_code', '?')} {result.get('description', 'unknown')}"
    )


def preflight(config: Config) -> int:
    """Check the configuration before the poll loop, so failures are one clear line."""
    problems: list[str] = []

    ok, message = verify_telegram_token(config)
    print(("  ok   " if ok else "  FAIL ") + message)
    if not ok:
        problems.append("telegram token")

    if config.mode == "cloud":
        try:
            get_json(
                f"{config.cloud_base_url}/api/v1/users/me",
                {"Authorization": f"Bearer {config.cloud_api_key}"},
            )
            print("  ok   openhands cloud key accepted")
        except Exception as exc:
            print(f"  FAIL openhands cloud key: {redact(str(exc), config.cloud_api_key)}")
            problems.append("openhands cloud key")

    if problems:
        print(f"\nFix the {' and '.join(problems)}, then run this again.")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


@dataclass
class State:
    offset: int | None = None
    conversations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "State":
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            return cls(
                offset=raw.get("offset"),
                conversations={str(k): str(v) for k, v in raw.get("conversations", {}).items()},
            )
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError):
            # A corrupt state file costs the mapping, not the process.
            return cls()

    def save(self, path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"offset": self.offset, "conversations": self.conversations}, handle, indent=2)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Command handling
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "HORIZON bridge\n"
    "/new — start a fresh agent conversation\n"
    "/status — show the current conversation state\n"
    "/id — show your Telegram user id\n"
    "/help — this text\n"
    "Any other message is sent to the agent."
)


def conversation_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


@dataclass
class Reply:
    text: str
    handled: bool


def handle_command(
    text: str,
    *,
    chat_id: int,
    user_id: int,
    state: State,
    backend: Backend,
) -> Reply | None:
    """Return a Reply for a slash command, or None when it is a normal message."""
    stripped = text.strip()
    key = conversation_key(chat_id, user_id)

    if stripped == "/help" or stripped == "/start":
        return Reply(HELP_TEXT, True)

    if stripped == "/id":
        return Reply(f"Your Telegram user id: {user_id}", True)

    if stripped == "/status":
        conversation_id = state.conversations.get(key)
        if not conversation_id:
            return Reply("No conversation yet. Send a message or /new.", True)
        try:
            detail = backend.status(conversation_id)
        except Exception as exc:  # surfaced, never fatal
            detail = f"unavailable ({exc})"
        return Reply(f"Conversation {conversation_id}\nStatus: {detail}", True)

    if stripped.startswith("/new"):
        first_message = stripped[len("/new") :].strip()
        if not first_message:
            return Reply("Usage: /new <first message>", True)
        conversation_id = backend.start(first_message)
        state.conversations[key] = conversation_id
        return Reply(f"Started {conversation_id}", True)

    return None


def route_message(
    text: str,
    *,
    chat_id: int,
    user_id: int,
    state: State,
    backend: Backend,
) -> list[str]:
    """Turn one authorized message into the replies to send back."""
    command = handle_command(text, chat_id=chat_id, user_id=user_id, state=state, backend=backend)
    if command is not None:
        return [command.text]

    key = conversation_key(chat_id, user_id)
    conversation_id = state.conversations.get(key)
    if conversation_id is None:
        conversation_id = backend.start(text)
        state.conversations[key] = conversation_id
    else:
        backend.send(conversation_id, text)
    return []


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def process_update(update: dict[str, Any], *, state: State, config: Config, backend: Backend) -> str | None:
    """Handle one Telegram update. Returns the reply text, or None for silence."""
    message = update.get("message")
    if not message:
        return None
    user_id = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")

    if not is_authorized(user_id, config.allowed_user_ids):
        return "Not authorized."
    if not isinstance(chat_id, int) or not text:
        return None

    replies = route_message(text, chat_id=chat_id, user_id=user_id, state=state, backend=backend)
    if replies:
        return replies[0]

    # The agent answers asynchronously; acknowledge so the operator knows it ran.
    return "Sent to the agent. Use /status to check."


def main() -> int:
    try:
        config = load_config()
        config.require_safe()
    except (ValueError, OSError) as exc:
        print(f"bridge: refusing to start: {exc}", file=sys.stderr)
        return 1

    backend = build_backend(config)
    telegram = Telegram(config.telegram_token)
    state = State.load(config.state_file)

    print("Checking the configuration...")
    if preflight(config) != 0:
        return 1

    print(f"\nbridge: up in {config.mode} mode; allowed users: {sorted(config.allowed_user_ids)}")
    while True:
        try:
            updates = telegram.get_updates(state.offset)
        except Exception as exc:
            print(f"bridge: poll error: {redact(str(exc), config.telegram_token)}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in updates:
            state.offset = update.get("update_id", 0) + 1
            state.save(config.state_file)
            try:
                reply = process_update(update, state=state, config=config, backend=backend)
            except Exception as exc:
                reply = f"Error: {redact(str(exc), config.telegram_token, config.cloud_api_key, config.local_session_api_key)}"
            if reply:
                chat_id = ((update.get("message") or {}).get("chat") or {}).get("id")
                if isinstance(chat_id, int):
                    try:
                        telegram.send_message(chat_id, reply)
                    except Exception as exc:
                        print(f"bridge: send error: {redact(str(exc), config.telegram_token)}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
