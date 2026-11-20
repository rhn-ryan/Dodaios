"""ChatGPT Web Integration: persistent conversational backend.

This provider turns ChatGPT's authenticated *web* chat surface into an optional,
reusable conversational backend for Dodaios. It does NOT rebuild or duplicate
the existing provider/call architecture: it implements the same ``Brain``
protocol (``complete`` / ``estimate_cost``) and is discovered through the same
``jarvis.brain`` entry-point slot, so it plugs into ``BrainManager`` like every
other brain.

Design (one persistent connection, many conversations):

* **Shared identity** — a single :class:`ChatGPTWebSession` holds the
  authenticated browser session and shared user preferences.
* **Conversation registry** — :class:`ConversationRegistry` keeps independent
  conversations (personal, development, research, troubleshooting, ...) each
  with their own history and purpose.
* **Context isolation** — each :class:`ChatGPTWebConversation` owns its own
  message log and running summary.
* **Shared memory** — common preferences/background context are injected
  selectively through ``shared_context``.
* **Routing** — callers pick a conversation by id or purpose; the backend
  never mixes histories.
* **Lifecycle** — create / resume / archive / switch conversations.
* **Context safeguards** — conversation size is monitored and a fresh
  conversation with a context handoff is started automatically when a limit is
  exceeded.
* **Portability** — Dodaios keeps its own conversation metadata and context
  summaries, so expiry of the browser session or a provider change does not
  lose the conversation.

The actual browser transport is deliberately abstracted behind
:class:`ChatGPTWebTransport`. The default implementation raises a clear error
so the provider is honest about its (currently unavailable) automation layer;
a real transport (persistent authenticated browser session / CDP / managed
profile) can be injected without changing this module. This keeps the
integration independent of the voice pipeline and of every other provider.

Terms-of-service note: driving ChatGPT's web UI through automation may be
restricted by OpenAI's terms. This module is shipped as an *optional*
backend; operators are responsible for ensuring their usage complies with the
provider's terms and platform restrictions. Feasibility, reliability,
authentication and ToS concerns must be reviewed before enabling it in
production.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterable, Mapping

import json as _json

import httpx

from dodaios.core.protocols import Brain, BrainDelta, BrainMessage, BrainRequest

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration + limits
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatGPTWebConfig:
    """Configurable safeguards and limits for the ChatGPT web backend."""

    # Per-message character cap sent to the web composer.
    max_input_chars: int = 4000
    # Conversation size (number of stored turns) before a handoff is triggered.
    max_conversation_turns: int = 60
    # Per-conversation character budget for the stored history.
    max_conversation_chars: int = 48_000
    # Bounded polling for a response.
    poll_interval_s: float = 1.0
    poll_timeout_s: float = 120.0
    max_polls: int = 120
    # Usage safeguards.
    max_requests_per_minute: int = 12
    max_concurrent_requests: int = 2
    duplicate_window_s: float = 10.0
    # Request tracking retention.
    request_retention: int = 256
    # HTTP transport settings (used by HttpChatGPTWebTransport).
    base_url: str = "https://chatgpt.com"
    model: str = "gpt-4o"
    cookies_file: str = ""
    access_token: str = ""
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatGPTWebMessage:
    role: str  # "user" | "assistant" | "system"
    content: str
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ChatGPTWebConversation:
    """One independent conversation with its own history and purpose."""

    conversation_id: str
    purpose: str = "general"
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[ChatGPTWebMessage] = field(default_factory=list)
    summary: str = ""
    archived: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return len(self.messages)

    @property
    def char_count(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def add_message(self, role: str, content: str, **metadata: Any) -> ChatGPTWebMessage:
        msg = ChatGPTWebMessage(role=role, content=content, metadata=dict(metadata))
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "purpose": self.purpose,
            "title": self.title,
            "turn_count": self.turn_count,
            "char_count": self.char_count,
            "archived": self.archived,
            "summary": self.summary,
            "updated_at": self.updated_at,
        }


class ConversationRegistry:
    """Registry of independent conversations sharing one identity."""

    def __init__(self) -> None:
        self._conversations: "OrderedDict[str, ChatGPTWebConversation]" = OrderedDict()
        self._active_id: str | None = None

    def create(
        self,
        *,
        purpose: str = "general",
        title: str = "",
        conversation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ChatGPTWebConversation:
        cid = conversation_id or f"conv-{uuid.uuid4().hex[:12]}"
        if cid in self._conversations:
            raise ValueError(f"conversation already exists: {cid}")
        conv = ChatGPTWebConversation(
            conversation_id=cid,
            purpose=purpose,
            title=title or purpose,
            metadata=dict(metadata or {}),
        )
        self._conversations[cid] = conv
        if self._active_id is None:
            self._active_id = cid
        return conv

    def get(self, conversation_id: str) -> ChatGPTWebConversation | None:
        return self._conversations.get(conversation_id)

    def require(self, conversation_id: str) -> ChatGPTWebConversation:
        conv = self.get(conversation_id)
        if conv is None:
            raise KeyError(f"unknown conversation: {conversation_id}")
        return conv

    def list(self, *, include_archived: bool = False) -> list[ChatGPTWebConversation]:
        out = []
        for c in self._conversations.values():
            if c.archived and not include_archived:
                continue
            out.append(c)
        return list(out)

    def active(self) -> ChatGPTWebConversation | None:
        if self._active_id is None:
            return None
        return self._conversations.get(self._active_id)

    def switch(self, conversation_id: str) -> ChatGPTWebConversation:
        conv = self.require(conversation_id)
        self._active_id = conversation_id
        return conv

    def archive(self, conversation_id: str) -> ChatGPTWebConversation:
        conv = self.require(conversation_id)
        conv.archived = True
        if self._active_id == conversation_id:
            # Pick another live conversation, if any.
            remaining = [c for c in self._conversations.values() if not c.archived]
            self._active_id = remaining[-1].conversation_id if remaining else None
        return conv

    def resume(self, conversation_id: str) -> ChatGPTWebConversation:
        conv = self.require(conversation_id)
        conv.archived = False
        self._active_id = conversation_id
        return conv

    def remove(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)
        if self._active_id == conversation_id:
            self._active_id = next(iter(self._conversations), None)

    def purposes(self) -> list[str]:
        return sorted({c.purpose for c in self._conversations.values()})


# ---------------------------------------------------------------------------
# Request tracking
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatGPTWebRequest:
    request_id: str
    conversation_id: str
    prompt: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | in_flight | done | error | cancelled
    response: str = ""
    error: str | None = None
    attempts: int = 0
    finished_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "response": self.response,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class RequestTracker:
    """Tracks in-flight and completed requests with bounded retention."""

    def __init__(self, retention: int = 256) -> None:
        self.retention = retention
        self._requests: "OrderedDict[str, ChatGPTWebRequest]" = OrderedDict()
        self._recent_prompts: deque[tuple[float, str, str]] = deque()  # (ts, conv, prompt)

    def new(self, conversation_id: str, prompt: str, **metadata: Any) -> ChatGPTWebRequest:
        rid = f"req-{uuid.uuid4().hex[:12]}"
        req = ChatGPTWebRequest(
            request_id=rid,
            conversation_id=conversation_id,
            prompt=prompt,
            metadata=dict(metadata),
        )
        self._requests[rid] = req
        self._trim()
        return req

    def get(self, request_id: str) -> ChatGPTWebRequest | None:
        return self._requests.get(request_id)

    def require(self, request_id: str) -> ChatGPTWebRequest:
        req = self.get(request_id)
        if req is None:
            raise KeyError(f"unknown request: {request_id}")
        return req

    def _trim(self) -> None:
        while len(self._requests) > self.retention:
            self._requests.popitem(last=False)

    # Duplicate-submission protection.
    def is_duplicate(self, conversation_id: str, prompt: str, window_s: float) -> bool:
        now = time.time()
        # prune
        while self._recent_prompts and now - self._recent_prompts[0][0] > window_s:
            self._recent_prompts.popleft()
        for ts, conv, p in self._recent_prompts:
            if conv == conversation_id and p == prompt and now - ts <= window_s:
                return True
        return False

    def record_submission(self, conversation_id: str, prompt: str) -> None:
        self._recent_prompts.append((time.time(), conversation_id, prompt))

    def rate_limit_ok(self, max_per_minute: int) -> bool:
        now = time.time()
        cutoff = now - 60.0
        recent = [r for r in self._requests.values() if r.created_at >= cutoff]
        return len(recent) < max_per_minute


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------


class ChatGPTWebTransport:
    """Abstract transport for sending a prompt and receiving a reply.

    The default implementation is honest: it reports that no automation layer
    is configured. A real implementation (persistent authenticated browser
    session, CDP, managed profile) overrides ``send_and_receive``.
    """

    async def is_authenticated(self) -> bool:
        return False

    async def send_and_receive(
        self,
        conversation_id: str,
        prompt: str,
        *,
        poll_interval_s: float,
        poll_timeout_s: float,
        max_polls: int,
    ) -> str:
        raise RuntimeError(
            "ChatGPT web transport is not configured. Provide an authenticated "
            "browser session (persistent profile / CDP) before using this backend."
        )

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# HTTP transport (ChatGPT web backend API)
# ---------------------------------------------------------------------------


class HttpChatGPTWebTransport(ChatGPTWebTransport):
    """Transport that talks to ChatGPT's web backend over HTTP.

    This reuses the existing HTTP stack (httpx) instead of duplicating a
    connectivity system. Authentication is based on the browser session
    cookies of a logged-in ChatGPT account; the access token is fetched from
    ``/api/auth/session``. Cookies can be persisted to ``cookies_file`` so the
    authenticated session survives restarts.

    Terms-of-service note: this drives ChatGPT's (undocumented) web backend.
    Operators must ensure their usage complies with OpenAI's terms.
    """

    def __init__(self, config: ChatGPTWebConfig | None = None) -> None:
        self.config = config or ChatGPTWebConfig()
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = self.config.access_token
        self._conversation_map: dict[str, tuple[str, str]] = {}  # dodaios conv -> (chatgpt conv id, parent msg id)
        self._cookies_loaded = False

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            cookies = None
            if self.config.cookies_file:
                try:
                    import pathlib

                    p = pathlib.Path(self.config.cookies_file)
                    if p.is_file():
                        data = _json.loads(p.read_text(encoding="utf-8"))
                        cookies = httpx.Cookies()
                        for c in data:
                            cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                except Exception:  # noqa: BLE001
                    log.debug("could not load cookies file", exc_info=True)
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                cookies=cookies,
                headers={"User-Agent": self.config.user_agent},
                timeout=httpx.Timeout(30.0, read=self.config.poll_timeout_s),
                follow_redirects=True,
            )
        return self._client

    async def _save_cookies(self) -> None:
        if not self.config.cookies_file or self._client is None:
            return
        try:
            import pathlib

            rows = []
            for cookie in self._client.cookies.jar:
                rows.append(
                    {
                        "name": cookie.name,
                        "value": cookie.value,
                        "domain": cookie.domain,
                    }
                )
            pathlib.Path(self.config.cookies_file).write_text(
                _json.dumps(rows), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            log.debug("could not save cookies file", exc_info=True)

    async def is_authenticated(self) -> bool:
        try:
            client = self._ensure_client()
            resp = await client.get("/api/auth/session")
            if resp.status_code != 200:
                return False
            data = resp.json()
            token = data.get("accessToken")
            if token:
                self._access_token = token
                await self._save_cookies()
                return True
            return False
        except Exception:  # noqa: BLE001
            log.debug("auth session probe failed", exc_info=True)
            return False

    async def _fetch_token(self) -> str:
        if self._access_token:
            return self._access_token
        client = self._ensure_client()
        resp = await client.get("/api/auth/session")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError("ChatGPT session is not authenticated")
        self._access_token = token
        await self._save_cookies()
        return token

    async def send_and_receive(
        self,
        conversation_id: str,
        prompt: str,
        *,
        poll_interval_s: float,
        poll_timeout_s: float,
        max_polls: int,
    ) -> str:
        token = await self._fetch_token()
        client = self._ensure_client()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        msg_id = str(__import__("uuid").uuid4())
        cg_conv_id, parent_id = self._conversation_map.get(conversation_id, (None, None))
        if parent_id is None:
            parent_id = str(__import__("uuid").uuid4())

        body: dict[str, Any] = {
            "action": "next",
            "messages": [
                {
                    "id": msg_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "parent_message_id": parent_id,
            "model": self.config.model,
            "timezone_offset_min": 0,
            "history_and_training_disabled": False,
            "conversation_mode": {"kind": "primary_assistant"},
            "force_paragen": False,
            "force_rate_limit": False,
        }
        if cg_conv_id:
            body["conversation_id"] = cg_conv_id

        collected: list[str] = []
        new_conv_id = cg_conv_id
        new_parent = parent_id
        async with client.stream(
            "POST", "/backend-api/conversation", headers=headers, json=body
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(
                    f"ChatGPT backend returned HTTP {resp.status_code}: {text[:300]!r}"
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = _json.loads(payload)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    if obj.get("conversation_id"):
                        new_conv_id = obj["conversation_id"]
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, dict):
                            parts = content.get("parts")
                            if isinstance(parts, list):
                                text = "".join(str(p) for p in parts if isinstance(p, str))
                                if text:
                                    collected = [text]
                        mid = msg.get("id")
                        if isinstance(mid, str):
                            new_parent = mid

        self._conversation_map[conversation_id] = (new_conv_id, new_parent)
        await self._save_cookies()
        if not collected:
            raise RuntimeError("ChatGPT web returned no response")
        return collected[-1] if collected else ""

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Session (shared identity)
# ---------------------------------------------------------------------------


class ChatGPTWebSession:
    """One authenticated ChatGPT session shared across conversations."""

    def __init__(
        self,
        config: ChatGPTWebConfig | None = None,
        transport: ChatGPTWebTransport | None = None,
        shared_context: str = "",
    ) -> None:
        self.config = config or ChatGPTWebConfig()
        self.transport = transport or ChatGPTWebTransport()
        self.shared_context = shared_context
        self.registry = ConversationRegistry()
        self.tracker = RequestTracker(retention=self.config.request_retention)
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        self._closed = False

    # ---- authentication ----
    async def is_authenticated(self) -> bool:
        return await self.transport.is_authenticated()

    # ---- safeguards ----
    def _truncate_input(self, text: str) -> str:
        if len(text) <= self.config.max_input_chars:
            return text
        return text[: self.config.max_input_chars]

    def _needs_handoff(self, conv: ChatGPTWebConversation) -> bool:
        return (
            conv.turn_count >= self.config.max_conversation_turns
            or conv.char_count >= self.config.max_conversation_chars
        )

    async def _handoff(self, conv: ChatGPTWebConversation) -> ChatGPTWebConversation:
        """Start a fresh conversation carrying a context summary."""
        summary = conv.summary or self._build_summary(conv)
        new_conv = self.registry.create(
            purpose=conv.purpose,
            title=f"{conv.title} (continued)",
            metadata={"continued_from": conv.conversation_id, "handoff_summary": summary},
        )
        if summary:
            new_conv.add_message("system", f"Context handoff from previous conversation:\n{summary}")
        new_conv.summary = summary
        log.info(
            "ChatGPT web: context handoff %s -> %s",
            conv.conversation_id,
            new_conv.conversation_id,
        )
        return new_conv

    @staticmethod
    def _build_summary(conv: ChatGPTWebConversation) -> str:
        lines = [f"Purpose: {conv.purpose}"]
        if conv.title:
            lines.append(f"Title: {conv.title}")
        # Keep a compact tail of the history.
        tail = conv.messages[-8:]
        for m in tail:
            lines.append(f"{m.role}: {m.content[:200]}")
        return "\n".join(lines)

    # ---- submission ----
    async def submit(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        purpose: str = "general",
        title: str = "",
        allow_duplicate: bool = False,
    ) -> ChatGPTWebRequest:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        prompt = self._truncate_input(prompt)

        # Resolve / create conversation.
        if conversation_id is None:
            active = self.registry.active()
            if active is None or active.archived:
                conv = self.registry.create(purpose=purpose, title=title or purpose)
            else:
                conv = active
                if conv.purpose != purpose and purpose != "general":
                    conv = self.registry.create(purpose=purpose, title=title or purpose)
        else:
            conv = self.registry.require(conversation_id)

        # Context safeguard: hand off if too large.
        if self._needs_handoff(conv):
            conv = await self._handoff(conv)

        # Duplicate-submission protection.
        if not allow_duplicate and self.tracker.is_duplicate(
            conv.conversation_id, prompt, self.config.duplicate_window_s
        ):
            raise RuntimeError("duplicate submission within the protection window")

        # Usage safeguard: rate limit.
        if not self.tracker.rate_limit_ok(self.config.max_requests_per_minute):
            raise RuntimeError("request rate limit exceeded")

        req = self.tracker.new(conv.conversation_id, prompt)
        self.tracker.record_submission(conv.conversation_id, prompt)

        # Record the user turn now (continuity).
        conv.add_message("user", prompt, request_id=req.request_id)

        # Execute with bounded polling under concurrency limit.
        async with self._semaphore:
            req.status = "in_flight"
            try:
                if not await self.transport.is_authenticated():
                    raise RuntimeError("ChatGPT web session is not authenticated")
                response = await self.transport.send_and_receive(
                    conv.conversation_id,
                    self._compose_prompt(conv, prompt),
                    poll_interval_s=self.config.poll_interval_s,
                    poll_timeout_s=self.config.poll_timeout_s,
                    max_polls=self.config.max_polls,
                )
                response = (response or "").strip()
                req.response = response
                req.status = "done"
                req.finished_at = time.time()
                conv.add_message("assistant", response, request_id=req.request_id)
                # Update summary periodically.
                conv.summary = self._build_summary(conv)
                return req
            except Exception as exc:  # noqa: BLE001 - surfaced as request error
                req.status = "error"
                req.error = str(exc)
                req.finished_at = time.time()
                log.warning("ChatGPT web request failed: %s", exc)
                raise

    def _compose_prompt(self, conv: ChatGPTWebConversation, prompt: str) -> str:
        parts: list[str] = []
        if self.shared_context:
            parts.append(f"Shared context (user preferences):\n{self.shared_context}")
        if conv.summary and conv.messages and conv.messages[0].role == "system":
            # Include handoff summary already stored as first system message.
            pass
        # Recent context is already in the web conversation; only send the new prompt.
        parts.append(prompt)
        return "\n\n".join(parts)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.transport.close()

    # Expose registry helpers.
    def create_conversation(self, **kwargs: Any) -> ChatGPTWebConversation:
        return self.registry.create(**kwargs)

    def list_conversations(self, **kwargs: Any) -> list[ChatGPTWebConversation]:
        return self.registry.list(**kwargs)

    def switch_conversation(self, conversation_id: str) -> ChatGPTWebConversation:
        return self.registry.switch(conversation_id)

    def archive_conversation(self, conversation_id: str) -> ChatGPTWebConversation:
        return self.registry.archive(conversation_id)

    def resume_conversation(self, conversation_id: str) -> ChatGPTWebConversation:
        return self.registry.resume(conversation_id)


# ---------------------------------------------------------------------------
# Brain adapter
# ---------------------------------------------------------------------------


class ChatGPTWebBrain(Brain):
    """Brain adapter exposing the ChatGPT web backend through the Brain protocol."""

    name: str = "chatgpt-web"
    context_window: int = 32_000
    supports_tools: bool = False
    supports_vision: bool = False

    def __init__(
        self,
        model: str | None = None,
        config: ChatGPTWebConfig | None = None,
        transport: ChatGPTWebTransport | None = None,
        shared_context: str = "",
        conversation_id: str | None = None,
        http: bool = False,
    ) -> None:
        self._model = model or "chatgpt-web"
        if transport is None and http:
            transport = HttpChatGPTWebTransport(config)
        self.session = ChatGPTWebSession(
            config=config, transport=transport, shared_context=shared_context
        )
        self._conversation_id = conversation_id

    def _resolve_conversation_id(self) -> str | None:
        if self._conversation_id is not None:
            return self._conversation_id
        active = self.session.registry.active()
        return active.conversation_id if active is not None else None

    async def complete(self, req: BrainRequest) -> AsyncIterator[BrainDelta]:
        # Flatten the last user message.
        user_text = ""
        for m in reversed(req.messages):
            if getattr(m, "role", None) == "user" and isinstance(m.content, str):
                user_text = m.content
                break
        if not user_text:
            # Fall back to stringifying the last message.
            if req.messages:
                last = req.messages[-1]
                user_text = str(getattr(last, "content", "") or "")

        conv_id = self._resolve_conversation_id()
        try:
            request = await self.session.submit(
                user_text,
                conversation_id=conv_id,
                purpose="general",
            )
            self._conversation_id = request.conversation_id
            text = request.response
        except Exception as exc:  # noqa: BLE001 - surfaced as brain error
            log.warning("ChatGPTWebBrain.complete failed: %s", exc)
            raise

        # Respect message-length limits on output too.
        yield BrainDelta(content=text)
        yield BrainDelta(finish_reason="stop")
        yield BrainDelta(
            usage={
                "input_tokens": len(user_text) // 4,
                "output_tokens": len(text) // 4,
            }
        )

    def estimate_cost(self, req: BrainRequest) -> float:
        # Billed through the user's ChatGPT subscription / web session, not per token.
        return 0.0

    # Convenience proxies for callers that want the registry/session directly.
    @property
    def registry(self) -> ConversationRegistry:
        return self.session.registry

    async def close(self) -> None:
        await self.session.close()


# Aliases for alternative naming used by callers / tests.
ChatGPTWebBackend = ChatGPTWebBrain
ChatGPTWebProvider = ChatGPTWebBrain


__all__ = [
    "ChatGPTWebConfig",
    "ChatGPTWebMessage",
    "ChatGPTWebConversation",
    "ConversationRegistry",
    "ChatGPTWebRequest",
    "RequestTracker",
    "ChatGPTWebTransport",
    "HttpChatGPTWebTransport",
    "ChatGPTWebSession",
    "ChatGPTWebBrain",
    "ChatGPTWebBackend",
    "ChatGPTWebProvider",
]
