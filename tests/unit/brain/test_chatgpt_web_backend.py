from __future__ import annotations

import asyncio
import time

import pytest

from dodaios.plugins.brain.chatgpt_web import (
    ChatGPTWebBrain,
    ChatGPTWebConfig,
    ChatGPTWebConversation,
    ChatGPTWebSession,
    ChatGPTWebTransport,
    ConversationRegistry,
    RequestTracker,
)


def test_conversation_registry_lifecycle() -> None:
    reg = ConversationRegistry()
    c1 = reg.create(purpose="personal", title="Personal")
    c2 = reg.create(purpose="research", title="Research")
    assert reg.active() is c1
    reg.switch(c2.conversation_id)
    assert reg.active() is c2
    assert len(reg.list()) == 2
    reg.archive(c2.conversation_id)
    assert c2.archived is True
    assert reg.active() is c1
    reg.resume(c2.conversation_id)
    assert c2.archived is False


def test_conversation_handoff_on_size() -> None:
    cfg = ChatGPTWebConfig(max_conversation_turns=2, max_conversation_chars=10_000)
    session = ChatGPTWebSession(config=cfg)
    conv = session.create_conversation(purpose="general", title="G")
    conv.add_message("user", "hi")
    conv.add_message("assistant", "hello")
    conv.add_message("user", "again")
    assert session._needs_handoff(conv) is True


def test_request_tracker_duplicate_detection() -> None:
    tr = RequestTracker(retention=10)
    tr.record_submission("c1", "hello")
    assert tr.is_duplicate("c1", "hello", 10.0) is True
    assert tr.is_duplicate("c1", "other", 10.0) is False
    assert tr.is_duplicate("c2", "hello", 10.0) is False


def test_rate_limit() -> None:
    tr = RequestTracker(retention=10)
    for _ in range(3):
        tr.new("c1", "x")
    assert tr.rate_limit_ok(3) is False
    assert tr.rate_limit_ok(10) is True


@pytest.mark.asyncio
async def test_session_requires_auth() -> None:
    session = ChatGPTWebSession()
    with pytest.raises(RuntimeError):
        await session.submit("hello")


class _FakeTransport(ChatGPTWebTransport):
    def __init__(self) -> None:
        self.authenticated = True
        self.received: list[tuple[str, str]] = []

    async def is_authenticated(self) -> bool:
        return self.authenticated

    async def send_and_receive(self, conversation_id, prompt, *, poll_interval_s, poll_timeout_s, max_polls):
        self.received.append((conversation_id, prompt))
        return f"reply to: {prompt.splitlines()[-1]}"


@pytest.mark.asyncio
async def test_session_submit_creates_conversation_and_tracks() -> None:
    transport = _FakeTransport()
    session = ChatGPTWebSession(transport=transport)
    req = await session.submit("hello world", purpose="research", title="R")
    assert req.status == "done"
    assert req.response.startswith("reply to:")
    convs = session.list_conversations()
    assert len(convs) == 1
    assert convs[0].purpose == "research"
    assert convs[0].turn_count == 2
    assert len(session.tracker.get(req.request_id).request_id) > 0


@pytest.mark.asyncio
async def test_duplicate_submission_blocked() -> None:
    transport = _FakeTransport()
    session = ChatGPTWebSession(transport=transport)
    await session.submit("same prompt")
    with pytest.raises(RuntimeError):
        await session.submit("same prompt")


@pytest.mark.asyncio
async def test_input_truncation() -> None:
    transport = _FakeTransport()
    cfg = ChatGPTWebConfig(max_input_chars=5)
    session = ChatGPTWebSession(config=cfg, transport=transport)
    req = await session.submit("abcdefghij")
    assert transport.received[-1][1].endswith("abcde") or "abcde" in transport.received[-1][1]


@pytest.mark.asyncio
async def test_brain_complete() -> None:
    transport = _FakeTransport()
    brain = ChatGPTWebBrain(transport=transport)
    from dodaios.core.protocols import BrainRequest, BrainMessage

    req = BrainRequest(messages=(BrainMessage(role="user", content="hi there"),))
    deltas = [d async for d in brain.complete(req)]
    assert any(d.content and d.content.startswith("reply to:") for d in deltas)
    assert any(d.finish_reason == "stop" for d in deltas)


@pytest.mark.asyncio
async def test_context_handoff_creates_new_conversation() -> None:
    transport = _FakeTransport()
    cfg = ChatGPTWebConfig(max_conversation_turns=1, max_conversation_chars=10_000)
    session = ChatGPTWebSession(config=cfg, transport=transport)
    conv = session.create_conversation(purpose="general", title="G")
    conv.add_message("user", "old")
    conv.add_message("assistant", "old reply")
    # now turn_count=2 >= max 1 -> handoff on submit
    req = await session.submit("new msg", conversation_id=conv.conversation_id)
    assert req.conversation_id != conv.conversation_id
    new_conv = session.registry.get(req.conversation_id)
    assert new_conv is not None
    assert new_conv.metadata.get("continued_from") == conv.conversation_id
