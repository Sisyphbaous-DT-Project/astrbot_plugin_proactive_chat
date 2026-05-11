from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from core.llm_adapter import LlmMixin
from core.message_sender import SenderMixin
from core.runtime_context_cache import RuntimeContextCache, RuntimeContextCacheMixin


class FakeEvent:
    def __init__(
        self,
        *,
        text: str,
        sender_id: str,
        sender_name: str,
        message_id: str,
        unified_msg_origin: str = "",
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            sender=SimpleNamespace(user_id=sender_id, nickname=sender_name),
        )

    def get_message_str(self) -> str:
        return self.message_str

    def get_message_outline(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname

    def get_result(self):
        return None


class RuntimeCacheHarness(RuntimeContextCacheMixin):
    def __init__(self, data_dir: Path | None = None) -> None:
        self.runtime_context_cache = RuntimeContextCache()
        self.timezone = ZoneInfo("Asia/Shanghai")
        self.telemetry = None
        self.session_data: dict = {}
        self._session_configs: dict[str, dict] = {}
        self.config: dict = {}
        self.data_dir = data_dir or Path.cwd()
        self.runtime_cache_file = self.data_dir / "runtime_context_cache.json"
        self.runtime_cache_dirty_sessions: set[str] = set()
        self.runtime_cache_save_task = None
        self.runtime_cache_save_delay_seconds = 0.0

    def _get_session_config(self, session_id: str) -> dict:
        return self._session_configs.get(session_id, {})

    def _get_context_settings(self, session_id: str) -> dict:
        session_config = self._get_session_config(session_id)
        if not isinstance(session_config, dict):
            return {}
        context_settings = session_config.get("context_settings")
        return context_settings if isinstance(context_settings, dict) else {}

    def _normalize_session_id(self, session_id: str) -> str:
        return session_id

    def _get_session_log_str(self, session_id: str, session_config: dict | None = None) -> str:
        return session_id

    def _parse_bool_setting(self, value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _sanitize_platform_context_text(self, text: str) -> str:
        return " ".join(str(text).split())


class HistoryHarness(LlmMixin, RuntimeCacheHarness):
    def __init__(self) -> None:
        RuntimeCacheHarness.__init__(self)

    async def _load_platform_message_history_records(
        self,
        session_id: str,
        limit: int,
    ) -> tuple[list[dict], int]:
        del session_id, limit
        return [], 0

    def _format_platform_history_as_context(
        self,
        records,
        *,
        include_bot_messages: bool,
        bot_identifiers: set[str],
        max_chars: int,
        context_settings: dict | None = None,
        unanswered_count: int = 0,
    ) -> tuple[dict | None, int, int]:
        del (
            records,
            include_bot_messages,
            bot_identifiers,
            max_chars,
            context_settings,
            unanswered_count,
        )
        return None, 0, 0


class FakeConversation:
    def __init__(self, history: list | str | None = None) -> None:
        self.history = history or []
        self.persona_id = None


class FakeConversationManager:
    def __init__(self) -> None:
        self.current: dict[str, str] = {}
        self.conversations: dict[str, FakeConversation] = {}
        self.switch_calls: list[tuple[str, str]] = []
        self.created_for: list[str] = []

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        return self.current.get(unified_msg_origin)

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
    ) -> str:
        del platform_id
        conv_id = f"conv-{len(self.conversations) + 1}"
        self.current[unified_msg_origin] = conv_id
        self.conversations[conv_id] = FakeConversation([])
        self.created_for.append(unified_msg_origin)
        return conv_id

    async def switch_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
    ) -> None:
        self.current[unified_msg_origin] = conversation_id
        self.switch_calls.append((unified_msg_origin, conversation_id))

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> FakeConversation | None:
        del unified_msg_origin
        conversation = self.conversations.get(conversation_id)
        if conversation is None and create_if_not_exists:
            conversation = FakeConversation([])
            self.conversations[conversation_id] = conversation
        return conversation

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
    ) -> None:
        conv_id = conversation_id or self.current.get(unified_msg_origin)
        if not conv_id:
            return
        conversation = self.conversations.setdefault(conv_id, FakeConversation([]))
        conversation.history = history or []

    async def add_message_pair(
        self,
        cid: str,
        user_message,
        assistant_message,
    ) -> None:
        conversation = self.conversations.setdefault(cid, FakeConversation([]))
        history = conversation.history
        if isinstance(history, str):
            history = []
        history.append(
            {
                "role": "user",
                "content": [{"text": user_message.content[0].text}],
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": [{"text": assistant_message.content[0].text}],
            }
        )
        conversation.history = history


class FakePersonaManager:
    async def get_default_persona_v3(self, umo: str) -> dict:
        del umo
        return {"prompt": "默认人格"}

    async def get_persona(self, persona_id: str):
        del persona_id
        return None


class SenderHarness(SenderMixin, RuntimeCacheHarness):
    def __init__(self) -> None:
        RuntimeCacheHarness.__init__(self)
        self.context = SimpleNamespace(
            get_using_tts_provider=lambda umo=None: None,
        )
        self.telemetry = None
        self._send_chain_with_hooks = AsyncMock(return_value=True)
        self._reset_group_silence_timer = AsyncMock()

    def _get_session_config(self, session_id: str) -> dict:
        return self._session_configs.get(
            session_id,
            {
                "enable": True,
                "_session_type": "private",
                "tts_settings": {"enable_tts": False, "always_send_text": True},
                "segmented_reply_settings": {"enable": False},
            },
        )




@pytest.mark.asyncio
async def test_runtime_cache_tracks_private_and_group_rounds() -> None:
    plugin = RuntimeCacheHarness()

    private_session = "aiocqhttp:FriendMessage:10001"
    group_session = "aiocqhttp:GroupMessage:20001"
    plugin._session_configs[private_session] = {"enable": True}
    plugin._session_configs[group_session] = {"enable": True}

    await plugin._cache_runtime_private_user_message(
        FakeEvent(
            text="第一轮私聊用户消息",
            sender_id="u1",
            sender_name="用户甲",
            message_id="p1",
        ),
        private_session,
        private_session,
    )
    await plugin._cache_runtime_bot_message_direct(
        session_id=private_session,
        text="第一轮私聊机器人回复",
    )
    await plugin._cache_runtime_private_user_message(
        FakeEvent(
            text="第二轮私聊用户消息",
            sender_id="u1",
            sender_name="用户甲",
            message_id="p2",
        ),
        private_session,
        private_session,
    )
    await plugin._cache_runtime_bot_message_direct(
        session_id=private_session,
        text="第二轮私聊机器人回复",
    )

    private_records, _ = plugin._load_runtime_context_cache_records(
        private_session,
        rounds=1,
        include_bot_messages=True,
    )
    assert [record.text for record in private_records] == [
        "第二轮私聊用户消息",
        "第二轮私聊机器人回复",
    ]
    assert [record.round_id for record in private_records] == [2, 2]

    await plugin._cache_runtime_group_member_message(
        FakeEvent(
            text="第一轮群聊成员发言",
            sender_id="m1",
            sender_name="群成员甲",
            message_id="g1",
        ),
        group_session,
        group_session,
    )
    await plugin._cache_runtime_bot_message_direct(
        session_id=group_session,
        text="第一轮群聊机器人回应",
    )
    await plugin._cache_runtime_group_member_message(
        FakeEvent(
            text="第二轮群聊成员发言",
            sender_id="m2",
            sender_name="群成员乙",
            message_id="g2",
        ),
        group_session,
        group_session,
    )
    await plugin._cache_runtime_bot_message_direct(
        session_id=group_session,
        text="第二轮群聊机器人回应",
    )

    group_records, _ = plugin._load_runtime_context_cache_records(
        group_session,
        rounds=1,
        include_bot_messages=True,
    )
    assert [record.text for record in group_records] == [
        "第二轮群聊成员发言",
        "第二轮群聊机器人回应",
    ]
    assert [record.round_id for record in group_records] == [2, 2]


@pytest.mark.asyncio
async def test_effective_history_prefers_runtime_cache_when_configured() -> None:
    plugin = HistoryHarness()
    session_id = "aiocqhttp:FriendMessage:30001"
    plugin._session_configs[session_id] = {"enable": True}

    await plugin._cache_runtime_private_user_message(
        FakeEvent(
            text="缓存里的最近消息",
            sender_id="u3",
            sender_name="用户丙",
            message_id="c1",
        ),
        session_id,
        session_id,
    )

    effective_history = await plugin._build_effective_history_context(
        session_id=session_id,
        conversation_history=[{"role": "assistant", "content": "旧对话"}],
        context_settings={
            "source_mode": "platform_message_history",
            "platform_history_count": 20,
            "platform_history_prompt": "",
            "include_bot_messages": True,
            "bot_identifiers": {"bot"},
            "platform_context_max_chars": 4000,
            "runtime_cache_enable": True,
            "runtime_cache_rounds": 10,
            "runtime_cache_max_chars": 4000,
            "cache_source_policy": "cache_first",
            "runtime_cache_prompt": "",
        },
        unanswered_count=1,
    )

    assert len(effective_history) == 1
    assert effective_history[0]["role"] == "system"
    assert "缓存里的最近消息" in effective_history[0]["content"]
    assert "旧对话" not in effective_history[0]["content"]


@pytest.mark.asyncio
async def test_event_cache_cache_only_returns_empty_without_records() -> None:
    plugin = HistoryHarness()
    session_id = "aiocqhttp:FriendMessage:30002"
    plugin._session_configs[session_id] = {"enable": True}

    effective_history = await plugin._build_effective_history_context(
        session_id=session_id,
        conversation_history=[{"role": "assistant", "content": "旧对话"}],
        context_settings={
            "source_mode": "event_cache",
            "platform_history_count": 20,
            "platform_history_prompt": "",
            "include_bot_messages": True,
            "bot_identifiers": {"bot"},
            "platform_context_max_chars": 4000,
            "runtime_cache_enable": True,
            "runtime_cache_rounds": 10,
            "runtime_cache_max_chars": 4000,
            "cache_source_policy": "cache_only",
            "runtime_cache_prompt": "",
        },
        unanswered_count=0,
    )

    assert effective_history == []


@pytest.mark.asyncio
async def test_prepare_llm_request_uses_last_event_umo_conversation() -> None:
    plugin = HistoryHarness()
    normalized_session = "default:FriendMessage:70001"
    raw_event_session = "aiocqhttp:FriendMessage:70001"
    conv_id = "conv-raw"

    plugin.session_data[normalized_session] = {
        "last_event_umo": raw_event_session,
        "unanswered_count": 0,
    }
    plugin._session_configs[normalized_session] = {
        "enable": True,
        "context_settings": {
            "source_mode": "conversation_history",
        },
    }
    plugin._session_configs[raw_event_session] = plugin._session_configs[
        normalized_session
    ]

    conv_mgr = FakeConversationManager()
    conv_mgr.current[raw_event_session] = conv_id
    conv_mgr.conversations[conv_id] = FakeConversation(
        [{"role": "user", "content": "用户真实对话历史"}]
    )
    plugin.context = SimpleNamespace(
        conversation_manager=conv_mgr,
        persona_manager=FakePersonaManager(),
    )

    request = await plugin._prepare_llm_request(normalized_session)

    assert request is not None
    assert request["conv_id"] == conv_id
    assert request["session_id"] == raw_event_session
    assert conv_mgr.current[raw_event_session] == conv_id
    assert conv_mgr.current[normalized_session] == conv_id
    assert request["history"] == [{"role": "user", "content": "用户真实对话历史"}]


@pytest.mark.asyncio
async def test_send_proactive_message_records_runtime_cache_after_success() -> None:
    plugin = SenderHarness()
    session_id = "aiocqhttp:FriendMessage:40001"
    plugin._session_configs[session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }

    await plugin._send_proactive_message(session_id, "测试主动消息写入缓存")

    plugin._send_chain_with_hooks.assert_awaited_once()
    records, record_count = plugin._load_runtime_context_cache_records(
        session_id,
        rounds=1,
        include_bot_messages=True,
    )
    assert record_count == 1
    assert records[0].text == "测试主动消息写入缓存"
    assert records[0].role == "assistant"
    assert records[0].source == "proactive_send"


@pytest.mark.asyncio
async def test_send_proactive_message_appends_assistant_to_astrbot_conversation() -> None:
    plugin = SenderHarness()
    raw_session_id = "aiocqhttp:FriendMessage:40002"
    normalized_session_id = "default:FriendMessage:40002"
    plugin._session_configs[normalized_session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }
    plugin._normalize_session_id = lambda sid: (
        normalized_session_id if sid in {raw_session_id, normalized_session_id} else sid
    )

    conv_mgr = FakeConversationManager()
    conv_id = await conv_mgr.new_conversation(raw_session_id, platform_id="aiocqhttp")
    conv_mgr.conversations[conv_id].history = json.dumps(
        [{"role": "user", "content": "之前的用户消息"}],
        ensure_ascii=False,
    )
    plugin.session_data[normalized_session_id] = {"last_event_umo": raw_session_id}
    plugin.context = SimpleNamespace(
        get_using_tts_provider=lambda umo=None: None,
        conversation_manager=conv_mgr,
    )

    await plugin._send_proactive_message(normalized_session_id, "这是一条主动消息")

    plugin._send_chain_with_hooks.assert_awaited_once()
    assert conv_mgr.current[raw_session_id] == conv_id
    assert conv_mgr.current[normalized_session_id] == conv_id
    history = conv_mgr.conversations[conv_id].history
    assert history == [
        {"role": "user", "content": "之前的用户消息"},
        {"role": "assistant", "content": "这是一条主动消息"},
    ]

    written_again = await plugin._persist_proactive_message_to_conversation_history(
        normalized_session_id,
        "这是一条主动消息",
    )
    assert written_again is False
    assert conv_mgr.conversations[conv_id].history == history


@pytest.mark.asyncio
async def test_runtime_cache_persists_and_restores_messages(tmp_path: Path) -> None:
    session_id = "aiocqhttp:FriendMessage:60001"

    plugin = RuntimeCacheHarness(tmp_path)
    plugin.session_data[session_id] = {}
    plugin._session_configs[session_id] = {
        "enable": True,
        "context_settings": {
            "runtime_cache_settings": {
                "enable": True,
                "persist_cache": True,
                "cache_storage_max_messages": 50,
            }
        },
    }

    for index in range(1, 27):
        await plugin._cache_runtime_private_user_message(
            FakeEvent(
                text=f"第{index}轮用户消息",
                sender_id="u6",
                sender_name="用户六",
                message_id=f"persist-u{index}",
            ),
            session_id,
            session_id,
        )
        await plugin._cache_runtime_bot_message_direct(
            session_id=session_id,
            text=f"第{index}轮机器人消息",
            message_id=f"persist-b{index}",
        )
    await plugin._flush_runtime_context_cache_save()

    restored = RuntimeCacheHarness(tmp_path)
    restored.session_data[session_id] = {}
    restored._session_configs[session_id] = plugin._session_configs[session_id]
    await restored._load_runtime_context_cache_from_disk()

    records, record_count = restored._load_runtime_context_cache_records(
        session_id,
        rounds=30,
        include_bot_messages=True,
    )
    assert record_count == 50
    assert records[0].text == "第2轮用户消息"
    assert records[-1].text == "第26轮机器人消息"
