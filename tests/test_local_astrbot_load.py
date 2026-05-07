from __future__ import annotations

import json
import shutil
import sys
import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from astrbot.core.star.star import star_registry
from astrbot.core.star.star_manager import PluginManager
from zoneinfo import ZoneInfo


class MockContext:
    def __init__(self) -> None:
        self.stars = []
        self._star_manager = None

    def get_all_stars(self):
        return self.stars

    def get_registered_star(self, name):
        for star in self.stars:
            if getattr(star, "root_dir_name", None) == name or getattr(star, "name", None) == name:
                return star
        return None

    def get_config(self, umo: str | None = None):
        del umo
        return {}


class FakeEvent:
    def __init__(self, text: str, message_id: str) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            sender=SimpleNamespace(user_id="tester", nickname="联调用户"),
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


class FakePersonaManager:
    async def get_default_persona_v3(self, umo: str) -> dict[str, str]:
        del umo
        return {"prompt": "默认人格"}

    async def get_persona(self, persona_id: str):
        del persona_id
        return None


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append(
            {
                "func": func,
                "trigger": trigger,
                **kwargs,
            }
        )


@pytest.mark.asyncio
async def test_plugin_can_load_through_local_astrbot_plugin_manager(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_source_root = Path(__file__).resolve().parents[1]
    astrbot_root = tmp_path / "astrbot_root"
    plugin_store_path = astrbot_root / "data" / "plugins"
    config_path = astrbot_root / "data" / "config"
    plugin_target_path = plugin_store_path / "astrbot_plugin_proactive_chat"

    plugin_store_path.mkdir(parents=True, exist_ok=True)
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / "astrbot_plugin_proactive_chat_config.json").write_text(
        json.dumps(
            {
                "web_admin": {"enabled": False},
                "notification_settings": {"enabled": False},
                "telemetry_config": {"enabled": False},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copytree(
        plugin_source_root,
        plugin_target_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "tests",
        ),
    )

    monkeypatch.setenv("ASTRBOT_ROOT", str(astrbot_root))
    sys.path.insert(0, str(astrbot_root))
    try:
        context = MockContext()
        plugin_manager = PluginManager(cast(Any, context), cast(Any, {}))
        monkeypatch.setattr(plugin_manager, "plugin_store_path", str(plugin_store_path))
        monkeypatch.setattr(
            "astrbot.core.star.star_manager.get_astrbot_plugin_path",
            lambda: str(plugin_store_path),
        )

        success, error = await plugin_manager.load(
            specified_dir_name="astrbot_plugin_proactive_chat",
        )

        assert success is True
        assert error is None
        metadata = next(
            star
            for star in star_registry
            if star.root_dir_name == "astrbot_plugin_proactive_chat"
        )
        assert metadata.star_cls is not None
        assert hasattr(metadata.star_cls, "runtime_context_cache")
    finally:
        if str(astrbot_root) in sys.path:
            sys.path.remove(str(astrbot_root))


@pytest.mark.asyncio
async def test_loaded_plugin_instance_runs_runtime_cache_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_source_root = Path(__file__).resolve().parents[1]
    astrbot_root = tmp_path / "astrbot_root"
    plugin_store_path = astrbot_root / "data" / "plugins"
    config_path = astrbot_root / "data" / "config"
    plugin_target_path = plugin_store_path / "astrbot_plugin_proactive_chat"

    plugin_store_path.mkdir(parents=True, exist_ok=True)
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / "astrbot_plugin_proactive_chat_config.json").write_text(
        json.dumps(
            {
                "web_admin": {"enabled": False},
                "notification_settings": {"enabled": False},
                "telemetry_config": {"enabled": False},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copytree(
        plugin_source_root,
        plugin_target_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "tests",
        ),
    )

    monkeypatch.setenv("ASTRBOT_ROOT", str(astrbot_root))
    sys.path.insert(0, str(astrbot_root))
    try:
        context = MockContext()
        plugin_manager = PluginManager(cast(Any, context), cast(Any, {}))
        monkeypatch.setattr(plugin_manager, "plugin_store_path", str(plugin_store_path))
        monkeypatch.setattr(
            "astrbot.core.star.star_manager.get_astrbot_plugin_path",
            lambda: str(plugin_store_path),
        )

        success, error = await plugin_manager.load(
            specified_dir_name="astrbot_plugin_proactive_chat",
        )

        assert success is True
        assert error is None
        metadata = next(
            star
            for star in star_registry
            if star.root_dir_name == "astrbot_plugin_proactive_chat"
        )
        plugin = metadata.star_cls
        assert plugin is not None

        session_id = "aiocqhttp:FriendMessage:50001"
        plugin.timezone = ZoneInfo("Asia/Shanghai")
        plugin.context.get_using_tts_provider = lambda umo=None: None
        plugin._normalize_session_id = MethodType(lambda self, sid: sid, plugin)
        plugin._send_chain_with_hooks = AsyncMock(return_value=True)
        plugin._reset_group_silence_timer = AsyncMock()

        def _get_session_config(self, sid: str) -> dict:
            if sid != session_id:
                return {}
            return {
                "enable": True,
                "_session_type": "private",
                "tts_settings": {"enable_tts": False, "always_send_text": True},
                "segmented_reply_settings": {"enable": False},
                "context_settings": {
                    "source_mode": "event_cache",
                    "include_bot_messages": True,
                    "runtime_cache_settings": {
                        "enable": True,
                        "cache_rounds": 10,
                        "cache_max_chars": 4000,
                        "cache_source_policy": "cache_first",
                    },
                },
            }

        plugin._get_session_config = MethodType(_get_session_config, plugin)

        await plugin._cache_runtime_private_user_message(
            FakeEvent("联调阶段的用户消息", "e2e-user-1"),
            session_id,
            session_id,
        )
        history = await plugin._build_effective_history_context(
            session_id=session_id,
            conversation_history=[],
            unanswered_count=0,
        )
        assert len(history) == 1
        assert "联调阶段的用户消息" in history[0]["content"]

        await plugin._send_proactive_message(session_id, "联调阶段的主动消息")
        plugin._send_chain_with_hooks.assert_awaited_once()

        records, _ = plugin._load_runtime_context_cache_records(
            session_id,
            rounds=2,
            include_bot_messages=True,
        )
        assert [record.text for record in records] == [
            "联调阶段的用户消息",
            "联调阶段的主动消息",
        ]
    finally:
        if str(astrbot_root) in sys.path:
            sys.path.remove(str(astrbot_root))


@pytest.mark.asyncio
async def test_loaded_plugin_instance_restores_persisted_runtime_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_source_root = Path(__file__).resolve().parents[1]
    astrbot_root = tmp_path / "astrbot_root"
    plugin_store_path = astrbot_root / "data" / "plugins"
    config_path = astrbot_root / "data" / "config"
    plugin_target_path = plugin_store_path / "astrbot_plugin_proactive_chat"

    plugin_store_path.mkdir(parents=True, exist_ok=True)
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / "astrbot_plugin_proactive_chat_config.json").write_text(
        json.dumps(
            {
                "web_admin": {"enabled": False},
                "notification_settings": {"enabled": False},
                "telemetry_config": {"enabled": False},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copytree(
        plugin_source_root,
        plugin_target_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "tests",
        ),
    )

    monkeypatch.setenv("ASTRBOT_ROOT", str(astrbot_root))
    sys.path.insert(0, str(astrbot_root))
    try:
        context = MockContext()
        plugin_manager = PluginManager(cast(Any, context), cast(Any, {}))
        monkeypatch.setattr(plugin_manager, "plugin_store_path", str(plugin_store_path))
        monkeypatch.setattr(
            "astrbot.core.star.star_manager.get_astrbot_plugin_path",
            lambda: str(plugin_store_path),
        )

        success, error = await plugin_manager.load(
            specified_dir_name="astrbot_plugin_proactive_chat",
        )

        assert success is True
        assert error is None
        metadata = next(
            star
            for star in star_registry
            if star.root_dir_name == "astrbot_plugin_proactive_chat"
        )
        plugin = metadata.star_cls
        assert plugin is not None

        session_id = "aiocqhttp:FriendMessage:50002"
        plugin.timezone = ZoneInfo("Asia/Shanghai")
        plugin.context.get_using_tts_provider = lambda umo=None: None
        plugin._normalize_session_id = MethodType(lambda self, sid: sid, plugin)

        def _get_session_config(self, sid: str) -> dict:
            if sid != session_id:
                return {}
            return {
                "enable": True,
                "_session_type": "private",
                "tts_settings": {"enable_tts": False, "always_send_text": True},
                "segmented_reply_settings": {"enable": False},
                "context_settings": {
                    "source_mode": "event_cache",
                    "include_bot_messages": True,
                    "runtime_cache_settings": {
                        "enable": True,
                        "persist_cache": True,
                        "cache_rounds": 10,
                        "cache_max_chars": 4000,
                        "cache_storage_max_messages": 20,
                        "cache_source_policy": "cache_first",
                    },
                },
            }

        plugin._get_session_config = MethodType(_get_session_config, plugin)
        plugin.session_data[session_id] = {}

        await plugin._cache_runtime_private_user_message(
            FakeEvent("准备写入持久化的用户消息", "persist-user-1"),
            session_id,
            session_id,
        )
        await plugin._cache_runtime_bot_message_direct(
            session_id=session_id,
            text="准备写入持久化的机器人消息",
            message_id="persist-bot-1",
        )
        await plugin._flush_runtime_context_cache_save()

        restored_plugin = plugin.__class__(plugin.context, plugin.config)
        restored_plugin.timezone = ZoneInfo("Asia/Shanghai")
        restored_plugin._normalize_session_id = MethodType(
            lambda self, sid: sid,
            restored_plugin,
        )
        restored_plugin._get_session_config = MethodType(
            _get_session_config,
            restored_plugin,
        )
        restored_plugin.session_data[session_id] = {}

        await restored_plugin._load_runtime_context_cache_from_disk()
        records, _ = restored_plugin._load_runtime_context_cache_records(
            session_id,
            rounds=10,
            include_bot_messages=True,
        )
        assert [record.text for record in records] == [
            "准备写入持久化的用户消息",
            "准备写入持久化的机器人消息",
        ]
    finally:
        if str(astrbot_root) in sys.path:
            sys.path.remove(str(astrbot_root))


@pytest.mark.asyncio
async def test_local_astrbot_conversation_history_keeps_proactive_message_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_source_root = Path(__file__).resolve().parents[1]
    astrbot_root = tmp_path / "astrbot_root"
    plugin_store_path = astrbot_root / "data" / "plugins"
    config_path = astrbot_root / "data" / "config"
    plugin_target_path = plugin_store_path / "astrbot_plugin_proactive_chat"

    plugin_store_path.mkdir(parents=True, exist_ok=True)
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / "astrbot_plugin_proactive_chat_config.json").write_text(
        json.dumps(
            {
                "web_admin": {"enabled": False},
                "notification_settings": {"enabled": False},
                "telemetry_config": {"enabled": False},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copytree(
        plugin_source_root,
        plugin_target_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "tests",
        ),
    )

    monkeypatch.setenv("ASTRBOT_ROOT", str(astrbot_root))
    sys.path.insert(0, str(astrbot_root))
    try:
        from astrbot.core.conversation_mgr import ConversationManager
        from astrbot.core.db.sqlite import SQLiteDatabase
        from astrbot.core.utils.shared_preferences import SharedPreferences
        import astrbot.core.conversation_mgr as conversation_mgr_module

        db_path = astrbot_root / "data" / "test_conversations.db"
        db = SQLiteDatabase(str(db_path))
        await db.initialize()
        temp_sp = SharedPreferences(
            db,
            json_storage_path=str(astrbot_root / "data" / "test_shared_preferences.json"),
        )
        monkeypatch.setattr(conversation_mgr_module, "sp", temp_sp)

        context = MockContext()
        plugin_manager = PluginManager(cast(Any, context), cast(Any, {}))
        monkeypatch.setattr(plugin_manager, "plugin_store_path", str(plugin_store_path))
        monkeypatch.setattr(
            "astrbot.core.star.star_manager.get_astrbot_plugin_path",
            lambda: str(plugin_store_path),
        )

        success, error = await plugin_manager.load(
            specified_dir_name="astrbot_plugin_proactive_chat",
        )

        assert success is True
        assert error is None
        metadata = next(
            star
            for star in star_registry
            if star.root_dir_name == "astrbot_plugin_proactive_chat"
        )
        plugin = metadata.star_cls
        assert plugin is not None

        raw_session_id = "aiocqhttp:FriendMessage:90001"
        normalized_session_id = "default:FriendMessage:90001"

        plugin.timezone = ZoneInfo("Asia/Shanghai")
        plugin.data_lock = asyncio.Lock()
        plugin.scheduler = FakeScheduler()
        plugin.context.conversation_manager = ConversationManager(db)
        plugin.context.persona_manager = FakePersonaManager()
        plugin.context.get_using_tts_provider = lambda umo=None: None
        plugin._send_chain_with_hooks = AsyncMock(return_value=True)
        plugin._normalize_session_id = MethodType(
            lambda self, sid: (
                normalized_session_id
                if sid in {raw_session_id, normalized_session_id}
                else sid
            ),
            plugin,
        )

        def _get_session_config(self, sid: str) -> dict:
            if sid not in {raw_session_id, normalized_session_id}:
                return {}
            return {
                "enable": True,
                "_session_type": "private",
                "tts_settings": {"enable_tts": False, "always_send_text": True},
                "segmented_reply_settings": {"enable": False},
                "context_settings": {
                    "source_mode": "conversation_history",
                },
                "schedule_settings": {
                    "min_interval_minutes": 30,
                    "max_interval_minutes": 30,
                },
            }

        plugin._get_session_config = MethodType(_get_session_config, plugin)

        raw_conv_id = await plugin.context.conversation_manager.new_conversation(
            raw_session_id,
            platform_id="aiocqhttp",
            content=[{"role": "user", "content": "用户之前说的话"}],
        )
        plugin.session_data[normalized_session_id] = {
            "last_event_umo": raw_session_id,
            "unanswered_count": 0,
        }

        request = await plugin._prepare_llm_request(normalized_session_id)
        assert request is not None
        assert request["conv_id"] == raw_conv_id
        assert request["session_id"] == raw_session_id

        normalized_conv_id = (
            await plugin.context.conversation_manager.get_curr_conversation_id(
                normalized_session_id
            )
        )
        assert normalized_conv_id == raw_conv_id

        await plugin._finalize_and_reschedule(
            session_id=request["session_id"],
            conv_id=request["conv_id"],
            user_prompt="系统任务生成的主动开场白",
            assistant_response="这是主动发出去的消息",
            unanswered_count=0,
        )

        user_side_conv_id = (
            await plugin.context.conversation_manager.get_curr_conversation_id(
                raw_session_id
            )
        )
        assert user_side_conv_id == raw_conv_id
        conversation = await plugin.context.conversation_manager.get_conversation(
            raw_session_id,
            user_side_conv_id,
        )
        assert conversation is not None
        history = json.loads(conversation.history)
        assert history[-2]["role"] == "user"
        assert history[-2]["content"][0]["text"] == "系统任务生成的主动开场白"
        assert history[-1]["role"] == "assistant"
        assert history[-1]["content"][0]["text"] == "这是主动发出去的消息"
    finally:
        if str(astrbot_root) in sys.path:
            sys.path.remove(str(astrbot_root))
