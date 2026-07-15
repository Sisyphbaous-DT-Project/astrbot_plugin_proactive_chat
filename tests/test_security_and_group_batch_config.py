from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from core.group_batch_config import (  # noqa: E402
    GroupBatchValidationError,
    normalize_group_batches,
    normalize_session_settings,
)
from core.llm_adapter import LlmMixin  # noqa: E402
from core import llm_adapter as llm_adapter_module  # noqa: E402
from core.session_config import ConfigMixin  # noqa: E402
from core.session_parser import SessionMixin  # noqa: E402
from core.task_scheduler import SchedulerMixin  # noqa: E402
from astrbot_plugin_proactive_chat import main as main_module  # noqa: E402


class CaptureLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _write(self, *args, **kwargs) -> None:
        del kwargs
        self.messages.append(" ".join(str(item) for item in args))

    debug = _write
    info = _write
    warning = _write
    error = _write


def test_session_log_description_does_not_include_identifiers_or_aliases() -> None:
    harness = object.__new__(SessionMixin)
    secret_id = "CHAT_SECRET_SESSION_ID_71"
    secret_alias = "CHAT_SECRET_SESSION_ALIAS_72"

    assert (
        harness._get_session_log_str(
            f"qq-main:FriendMessage:{secret_id}",
            {"session_name": secret_alias},
        )
        == "私聊会话"
    )
    assert (
        harness._get_session_log_str(
            f"qq-main:GuildMessage:{secret_id}",
            {"session_name": secret_alias},
        )
        == "群聊会话"
    )
    assert harness._get_session_log_str(secret_id) == "会话"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "不是列表",
        [{"session_list": None}],
        [{"group_idle_trigger_minutes": None}],
        [{"min_interval_minutes": True}],
        [{"quiet_hours": "这不是时间段"}],
        [{"min_interval_minutes": 120, "max_interval_minutes": 60}],
        ["错误批次", {"session_list": ["qq:GroupMessage:1"]}],
    ],
)
def test_group_batch_runtime_normalization_never_raises(raw) -> None:
    normalized = normalize_group_batches(raw)
    assert isinstance(normalized, list)
    assert all(isinstance(batch, dict) for batch in normalized)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "不是列表",
        [{"session_list": None}],
        [{"group_idle_trigger_minutes": None}],
        [{"min_interval_minutes": True}],
        [{"quiet_hours": "这不是时间段"}],
        [{"min_interval_minutes": 120, "max_interval_minutes": 60}],
    ],
)
def test_group_batch_web_validation_rejects_bad_input(raw) -> None:
    with pytest.raises(GroupBatchValidationError):
        normalize_group_batches(raw, strict=True)


def test_group_batch_runtime_normalization_keeps_valid_batch_after_bad_item() -> None:
    batches = normalize_group_batches(
        [
            "错误批次",
            {"batch_name": "正常批次", "session_list": [" qq:GroupMessage:1 ", ""]},
        ]
    )
    assert batches == [
        {
            "batch_name": "正常批次",
            "session_list": ["qq:GroupMessage:1"],
            "group_idle_trigger_minutes": 30,
            "min_interval_minutes": 90,
            "max_interval_minutes": 360,
            "quiet_hours": "2-6",
            "max_unanswered_times": 2,
            "proactive_prompt": "",
        }
    ]


def test_runtime_session_settings_fall_back_for_bad_scheduler_values() -> None:
    normalized = normalize_session_settings(
        {
            "enable": True,
            "session_list": [" 10001 ", "10001"],
            "auto_trigger_settings": None,
            "schedule_settings": {
                "min_interval_minutes": None,
                "max_interval_minutes": "bad",
                "max_unanswered_times": None,
            },
            "proactive_prompt": None,
        },
        session_type="friend",
    )

    assert normalized["session_list"] == ["10001"]
    assert normalized["auto_trigger_settings"] == {
        "enable_auto_trigger": False,
        "auto_trigger_after_minutes": 5,
    }
    assert normalized["schedule_settings"] == {
        "min_interval_minutes": 30,
        "max_interval_minutes": 900,
        "max_unanswered_times": 3,
        "quiet_hours": "1-7",
    }
    assert normalized["proactive_prompt"] == ""


def test_runtime_session_settings_repair_bad_message_settings() -> None:
    normalized = normalize_session_settings(
        {
            "context_settings": "bad",
            "tts_settings": "bad",
            "segmented_reply_settings": "bad",
        },
        session_type="friend",
    )

    assert normalized["context_settings"]["source_mode"] == "conversation_history"
    assert normalized["tts_settings"] == {
        "enable_tts": True,
        "always_send_text": True,
    }
    assert normalized["segmented_reply_settings"]["enable"] is False
    assert normalized["segmented_reply_settings"]["split_mode"] == "regex"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auto_trigger_settings", None),
        ("schedule_settings", {"min_interval_minutes": "bad"}),
        ("proactive_prompt", None),
        ("context_settings", "bad"),
        ("context_settings", {"runtime_cache_settings": None}),
        ("tts_settings", "bad"),
        ("tts_settings", {"enable_tts": "bad"}),
        ("segmented_reply_settings", "bad"),
        ("segmented_reply_settings", {"split_words": ["。", 1]}),
        ("segmented_reply_settings", {"log_base": "1"}),
    ],
)
def test_strict_session_settings_reject_bad_nested_values(field, value) -> None:
    with pytest.raises(GroupBatchValidationError):
        normalize_session_settings(
            {field: value},
            session_type="friend",
            strict=True,
            fill_defaults=False,
        )


@pytest.mark.asyncio
async def test_scheduler_setup_ignores_null_session_lists() -> None:
    class SchedulerHarness(SchedulerMixin):
        def __init__(self) -> None:
            self.config = {
                "friend_settings": {
                    "enable": True,
                    "session_list": None,
                },
                "group_settings": {
                    "enable": True,
                    "session_list": None,
                },
                "group_batches": [
                    {"session_list": None},
                    {"session_list": ["qq:GroupMessage:1"]},
                ],
            }

        async def _setup_auto_trigger_for_session_config(
            self, settings, session_id
        ) -> str:
            del settings, session_id
            return "invalid"

    # 如果任一入口仍直接遍历 null，这里会在初始化阶段抛 TypeError。
    await SchedulerHarness()._setup_auto_triggers_for_enabled_sessions()


@pytest.mark.asyncio
async def test_web_config_rejects_bad_batches_without_mutating_old_config() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    old_batches = [{"batch_name": "旧配置", "session_list": ["qq:GroupMessage:1"]}]
    plugin = SimpleNamespace(
        config={"group_batches": old_batches, "web_admin": {"enabled": False}},
        _astrbot_supports_template_list=lambda: False,
        _normalize_group_batches_for_runtime=lambda: None,
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
    )
    server = web_admin_module.WebAdminServer(plugin)
    route = next(
        route
        for route in server.app.routes
        if getattr(route, "path", None) == "/api/config"
        and "POST" in getattr(route, "methods", set())
    )

    response = await route.endpoint({"group_batches": [{"session_list": None}]})

    assert response.status_code == 400
    assert plugin.config["group_batches"] == old_batches
    plugin._save_plugin_config.assert_not_called()


@pytest.mark.asyncio
async def test_web_config_rejects_bad_nested_scheduler_values_without_mutation() -> (
    None
):
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    old_settings = {
        "enable": True,
        "session_list": ["qq:FriendMessage:1"],
        "schedule_settings": {
            "min_interval_minutes": 30,
            "max_interval_minutes": 60,
            "max_unanswered_times": 3,
        },
    }
    plugin = SimpleNamespace(
        config={
            "friend_settings": old_settings,
            "web_admin": {"enabled": False},
        },
        _astrbot_supports_template_list=lambda: False,
        _normalize_group_batches_for_runtime=lambda: None,
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
    )
    server = web_admin_module.WebAdminServer(plugin)
    route = next(
        route
        for route in server.app.routes
        if getattr(route, "path", None) == "/api/config"
        and "POST" in getattr(route, "methods", set())
    )

    response = await route.endpoint(
        {
            "friend_settings": {
                **old_settings,
                "schedule_settings": {"min_interval_minutes": None},
            }
        }
    )

    assert response.status_code == 400
    assert plugin.config["friend_settings"] == old_settings
    plugin._save_plugin_config.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_settings", "bad"),
        ("tts_settings", "bad"),
        ("segmented_reply_settings", "bad"),
    ],
)
async def test_web_config_rejects_bad_message_settings_without_mutation(
    field,
    value,
) -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    old_settings = {
        "enable": True,
        "session_list": ["qq:FriendMessage:1"],
        "tts_settings": {"enable_tts": False},
    }
    plugin = SimpleNamespace(
        config={
            "friend_settings": old_settings,
            "web_admin": {"enabled": False},
        },
        _astrbot_supports_template_list=lambda: False,
        _normalize_group_batches_for_runtime=lambda: None,
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
    )
    server = web_admin_module.WebAdminServer(plugin)
    route = next(
        route
        for route in server.app.routes
        if getattr(route, "path", None) == "/api/config"
        and "POST" in getattr(route, "methods", set())
    )

    response = await route.endpoint({"friend_settings": {**old_settings, field: value}})

    assert response.status_code == 400
    assert plugin.config["friend_settings"] == old_settings
    plugin._save_plugin_config.assert_not_called()


@pytest.mark.asyncio
async def test_web_config_keeps_partial_batch_fields_inheriting_global_defaults() -> (
    None
):
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    plugin = SimpleNamespace(
        config={
            "group_settings": {"group_idle_trigger_minutes": 77},
            "group_batches": [],
            "web_admin": {"enabled": False},
        },
        _astrbot_supports_template_list=lambda: False,
        _normalize_group_batches_for_runtime=lambda: None,
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
    )
    server = web_admin_module.WebAdminServer(plugin)
    route = next(
        route
        for route in server.app.routes
        if getattr(route, "path", None) == "/api/config"
        and "POST" in getattr(route, "methods", set())
    )

    response = await route.endpoint(
        {
            "group_batches": [
                {"batch_name": "局部批次", "session_list": ["qq:GroupMessage:1"]}
            ]
        }
    )

    assert response == {"ok": True}
    assert plugin.config["group_batches"] == [
        {"batch_name": "局部批次", "session_list": ["qq:GroupMessage:1"]}
    ]


def test_session_override_api_rejects_bad_nested_scheduler_values() -> None:
    import asyncio

    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    override_module = __import__(
        "core.session_override_manager", fromlist=["SessionOverrideManager"]
    )
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    manager = object.__new__(override_module.SessionOverrideManager)
    manager._overrides = {}
    manager._lock = asyncio.Lock()
    manager._save = AsyncMock()
    base = {
        "enable": True,
        "_session_type": "friend",
        "schedule_settings": {
            "min_interval_minutes": 30,
            "max_interval_minutes": 60,
            "max_unanswered_times": 3,
        },
    }
    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": False}},
        session_override_manager=manager,
        _normalize_session_id=lambda value: value,
        _parse_session_id=lambda value: tuple(value.split(":", 2)),
        _get_base_session_config=lambda _value: base,
        _get_session_config=lambda value: manager.get_effective(value, base),
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    client = TestClient(server.app)

    response = client.post(
        "/api/session-config/qq:FriendMessage:1",
        json={
            "mode": "override",
            "override": {"schedule_settings": {"min_interval_minutes": "bad"}},
        },
    )

    assert response.status_code == 400
    assert manager._overrides == {}


@pytest.mark.parametrize(
    "field",
    ["context_settings", "tts_settings", "segmented_reply_settings"],
)
def test_session_override_api_rejects_bad_message_settings(field) -> None:
    import asyncio

    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    override_module = __import__(
        "core.session_override_manager", fromlist=["SessionOverrideManager"]
    )
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    manager = object.__new__(override_module.SessionOverrideManager)
    manager._overrides = {}
    manager._lock = asyncio.Lock()
    manager._save = AsyncMock()
    base = {"enable": True, "_session_type": "friend"}
    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": False}},
        session_override_manager=manager,
        _normalize_session_id=lambda value: value,
        _parse_session_id=lambda value: tuple(value.split(":", 2)),
        _get_base_session_config=lambda _value: base,
        _get_session_config=lambda value: manager.get_effective(value, base),
        _save_plugin_config=Mock(),
        _broadcast_update=AsyncMock(),
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    client = TestClient(web_admin_module.WebAdminServer(plugin).app)

    response = client.post(
        "/api/session-config/qq:FriendMessage:1",
        json={"mode": "override", "override": {field: "bad"}},
    )

    assert response.status_code == 400
    assert manager._overrides == {}


@pytest.mark.asyncio
async def test_platform_history_and_provider_logs_do_not_include_exception_text(
    monkeypatch,
) -> None:
    capture = CaptureLogger()
    monkeypatch.setattr(llm_adapter_module, "logger", capture)
    secret = "CHAT_SECRET_PROVIDER_7b"

    class HistoryManager:
        async def get(self, **kwargs):
            del kwargs
            raise RuntimeError(f"平台流水失败：{secret}")

    history_harness = object.__new__(LlmMixin)
    history_harness.context = SimpleNamespace(message_history_manager=HistoryManager())
    history_harness._parse_umo_for_platform_history = lambda _session: (
        "qq",
        "10001",
    )
    history_harness._build_platform_history_user_candidates = lambda _user: ["10001"]
    records, count = await history_harness._load_platform_message_history_records(
        "qq:FriendMessage:10001", 10
    )
    assert records == []
    assert count == 0

    class Provider:
        async def text_chat(self, **kwargs):
            del kwargs
            raise RuntimeError(f"Provider拒绝：{secret}")

    class Context:
        async def get_current_chat_provider_id(self, _session):
            raise RuntimeError(f"新接口失败：{secret}")

        def get_using_provider(self, umo):
            del umo
            return Provider()

    llm_harness = object.__new__(LlmMixin)
    llm_harness.context = Context()
    llm_harness.timezone = timezone.utc
    result, _prompt = await llm_harness._generate_llm_response(
        "qq:FriendMessage:10001",
        {},
        [],
        "人格",
        0,
    )
    assert result is None
    assert capture.messages
    assert all(secret not in message for message in capture.messages)


def test_session_config_routes_private_and_guild_messages() -> None:
    class Harness(ConfigMixin):
        def __init__(self) -> None:
            self.config = {
                "friend_settings": {
                    "enable": True,
                    "session_list": ["10001"],
                },
                "group_settings": {
                    "enable": True,
                    "session_list": ["20001"],
                },
            }
            self.session_override_manager = None

        def _parse_session_id(self, session_id: str):
            parts = session_id.split(":", 2)
            return tuple(parts) if len(parts) == 3 else None

        def _normalize_session_id(self, session_id: str) -> str:
            return session_id

    harness = Harness()
    assert harness._get_base_session_config("qq:PrivateMessage:10001") is not None
    assert harness._get_base_session_config("qq:GuildMessage:20001") is not None


def test_session_config_runtime_bad_schedule_uses_defaults() -> None:
    class Harness(ConfigMixin):
        def __init__(self) -> None:
            self.config = {
                "friend_settings": {
                    "enable": True,
                    "session_list": ["10001"],
                    "auto_trigger_settings": None,
                    "schedule_settings": {
                        "min_interval_minutes": None,
                        "max_interval_minutes": "bad",
                        "max_unanswered_times": None,
                    },
                }
            }
            self.session_override_manager = None

        def _parse_session_id(self, session_id: str):
            parts = session_id.split(":", 2)
            return tuple(parts) if len(parts) == 3 else None

        def _normalize_session_id(self, session_id: str) -> str:
            return session_id

    config = Harness()._get_session_config("qq:FriendMessage:10001")
    assert config is not None
    assert config["auto_trigger_settings"]["enable_auto_trigger"] is False
    assert config["schedule_settings"]["min_interval_minutes"] == 30
    assert config["schedule_settings"]["max_interval_minutes"] == 900


def test_safe_logging_rejects_spoofed_builtin_exception_name() -> None:
    from utils.safe_logging import exception_type_name, log_safe_exception

    secret = "CHAT_SECRET_BUILTIN_SPOOF_TEST"
    spoofed_error_type = type(secret, (Exception,), {"__module__": "builtins"})
    error = spoofed_error_type("异常正文")

    assert exception_type_name(error) == "ExternalError"

    capture = CaptureLogger()
    log_safe_exception(capture, "error", "PC-TEST-DYNAMIC", secret, error)
    assert capture.messages
    assert secret not in repr(capture.messages)
    assert "PC-UNKNOWN" in capture.messages[0]


def test_safe_logging_handles_exception_type_with_failing_hash() -> None:
    from utils.safe_logging import exception_type_name, log_safe_exception

    secret = "CHAT_SECRET_METACLASS_HASH_TEST"

    class ErrorMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError(secret)

    class ExternalError(Exception, metaclass=ErrorMeta):
        pass

    error = ExternalError("异常正文")
    capture = CaptureLogger()

    assert exception_type_name(error) == "ExternalError"
    log_safe_exception(capture, "error", "PC-LLM-001", secret, error)

    assert capture.messages
    assert secret not in repr(capture.messages)
    assert "错误类型: ExternalError" in capture.messages[0]


@pytest.mark.asyncio
async def test_notification_stop_handles_unstarted_poll_task(tmp_path: Path) -> None:
    from core.notification_center import NotificationCenter

    plugin = SimpleNamespace(
        config={"notification_settings": {"enabled": True}},
        data_dir=tmp_path,
        web_admin_server=None,
    )
    center = NotificationCenter(plugin)
    center.load_cache = AsyncMock()
    center.refresh = AsyncMock(return_value=False)
    center.save_cache = AsyncMock()

    await center.start()
    await center.stop()

    assert center._poll_task is None
    center.save_cache.assert_awaited_once()


def test_background_task_failure_is_consumed_without_exception_text(
    monkeypatch,
) -> None:
    import asyncio

    capture = CaptureLogger()
    monkeypatch.setattr(main_module, "logger", capture)
    plugin = object.__new__(main_module.ProactiveChatPlugin)
    plugin._background_tasks = set()
    secret = "CHAT_SECRET_BACKGROUND_TASK"

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_future()
        task.set_exception(RuntimeError(secret))
        plugin._background_tasks.add(task)
        plugin._on_background_task_done(task)
    finally:
        loop.close()

    assert capture.messages
    assert secret not in repr(capture.messages)


@pytest.mark.asyncio
async def test_terminate_rejects_timer_task_created_during_cleanup() -> None:
    import asyncio

    from core.plugin_lifecycle import LifecycleMixin

    class LifecycleHarness(LifecycleMixin):
        _track_task = main_module.ProactiveChatPlugin._track_task
        _on_background_task_done = (
            main_module.ProactiveChatPlugin._on_background_task_done
        )
        _cleanup_background_tasks = (
            main_module.ProactiveChatPlugin._cleanup_background_tasks
        )

        def __init__(self) -> None:
            self._background_tasks = set()
            self.group_timers = {}
            self.auto_trigger_timers = {}
            self.scheduler = None
            self.data_lock = None
            self.web_admin_server = None
            self.notification_center = None

        def _get_session_log_str(self, session_id: str) -> str:
            return session_id

        async def _flush_runtime_context_cache_save(self) -> None:
            return None

    harness = LifecycleHarness()
    blocker = asyncio.Event()

    async def active_task() -> None:
        try:
            await blocker.wait()
        finally:
            await asyncio.sleep(0)

    async def late_task() -> None:
        await asyncio.Event().wait()

    active = harness._track_task(asyncio.create_task(active_task()))
    await asyncio.sleep(0)
    created_tasks: list[asyncio.Task] = []

    def timer_callback() -> None:
        created_tasks.append(harness._track_task(asyncio.create_task(late_task())))

    harness.group_timers["qq:GroupMessage:1"] = asyncio.get_running_loop().call_later(
        0,
        timer_callback,
    )
    try:
        await harness.terminate()
        await asyncio.sleep(0)
        assert active.cancelled()
        assert created_tasks == []
        assert harness._background_tasks == set()
        rejected_task = harness._track_task(asyncio.create_task(late_task()))
        await asyncio.sleep(0)
        assert rejected_task.cancelled()
    finally:
        for task in created_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*created_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_admin_manual_trigger_registers_background_task() -> None:
    import asyncio

    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    tracked_tasks: list[asyncio.Task] = []

    async def check_and_chat(_session_id: str) -> None:
        await asyncio.sleep(0)

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": False}},
        manual_trigger_sessions=set(),
        _normalize_session_id=lambda session_id: session_id,
        check_and_chat=check_and_chat,
        _track_task=tracked_tasks.append,
    )
    server = web_admin_module.WebAdminServer(plugin)
    server._broadcast_update = AsyncMock()
    route = next(
        route
        for route in server.app.routes
        if getattr(route, "path", None) == "/api/jobs/{umo:path}/trigger"
        and "POST" in getattr(route, "methods", set())
    )

    response = await route.endpoint("qq:FriendMessage:10001")
    assert response["ok"] is True
    assert len(tracked_tasks) == 1
    await tracked_tasks[0]


def _make_websocket_test_server(password: str = "secret"):
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    try:
        from fastapi.testclient import TestClient
    except Exception:
        pytest.skip("FastAPI TestClient is not available")

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": password}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    server._build_status_payload = lambda: {"ok": True}
    server._collect_jobs = lambda: []
    server._list_known_session_summaries = lambda: []

    async def notifications():
        return {"items": []}

    server._build_notification_payload = notifications
    token = "valid-token"
    server._tokens[token] = 9999999999
    return server, TestClient(server.app), token


def test_websocket_query_token_is_not_accepted() -> None:
    server, client, token = _make_websocket_test_server()

    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()

    assert server._ws_connections == []


def test_no_auth_token_is_rejected_when_password_is_enabled() -> None:
    server, client, _token = _make_websocket_test_server()

    assert server._verify_token("no-auth") is False
    response = client.get(
        "/api/status",
        headers={"Authorization": "Bearer no-auth"},
    )
    assert response.status_code == 401

    with pytest.raises(Exception):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "no-auth"})
            ws.receive_json()
    assert server._ws_connections == []


def test_password_change_updates_auth_and_invalidates_old_token() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": ""}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    server._build_status_payload = lambda: {"ok": True}
    server._collect_jobs = lambda: []
    server._list_known_session_summaries = lambda: []
    server._build_notification_payload = AsyncMock(return_value={"items": []})
    client = TestClient(server.app)

    first = client.post(
        "/api/config",
        json={"web_admin": {"password": "first-password"}},
    )
    assert first.status_code == 200
    assert server._auth_enabled is True
    assert client.get("/api/status").status_code == 401

    token = server._issue_token()
    second = client.post(
        "/api/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"web_admin": {"password": "second-password"}},
    )
    assert second.status_code == 200
    assert server._verify_token(token) is False

    latest_token = server._issue_token()
    third = client.post(
        "/api/config",
        headers={"Authorization": f"Bearer {latest_token}"},
        json={"web_admin": {"password": ""}},
    )
    assert third.status_code == 200
    assert server._auth_enabled is False
    assert client.get("/api/status").status_code == 200


def test_web_admin_rejects_non_object_or_non_string_password() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": ""}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    client = TestClient(server.app)

    assert client.post("/api/config", json={"web_admin": None}).status_code == 400
    assert (
        client.post(
            "/api/config",
            json={"web_admin": {"password": 123}},
        ).status_code
        == 400
    )


@pytest.mark.parametrize("invalid_password", [None, 123, False])
def test_invalid_existing_password_config_fails_closed(invalid_password) -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": invalid_password}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    client = TestClient(server.app)

    assert server._auth_enabled is True
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"password": "123"}).status_code == 503


def test_live_non_object_web_admin_config_fails_closed() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")
    from fastapi.testclient import TestClient

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": "secret"}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    server._build_status_payload = lambda: {"ok": True}
    client = TestClient(server.app)

    assert client.get("/api/status").status_code == 401
    plugin.config["web_admin"] = "corrupted-live-config"
    assert client.get("/api/status").status_code == 401
    assert server._auth_enabled is True


@pytest.mark.asyncio
async def test_password_rotation_closes_existing_websockets() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": "first-password"}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    websocket = SimpleNamespace(close=AsyncMock())
    server._ws_connections = [websocket]
    plugin.config["web_admin"]["password"] = "second-password"

    await server._sync_auth_state()

    websocket.close.assert_awaited_once_with(code=1008)
    assert server._ws_connections == []


def test_websocket_first_frame_auth_receives_initial_snapshot() -> None:
    server, client, token = _make_websocket_test_server()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        payload = ws.receive_json()
        assert payload["type"] == "full_update"
        assert payload["data"]["status"] == {"ok": True}

    assert server._ws_connections == []


def test_websocket_detects_out_of_band_password_rotation() -> None:
    server, client, token = _make_websocket_test_server()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "full_update"

        server.plugin.config["web_admin"]["password"] = "rotated-out-of-band"
        ws.send_json({"type": "refresh"})
        with pytest.raises(Exception):
            ws.receive_json()

    assert server._verify_token(token) is False
    assert server._ws_connections == []


@pytest.mark.asyncio
async def test_websocket_broadcast_detects_out_of_band_password_rotation() -> None:
    web_admin_module = __import__("core.web_admin_server", fromlist=["WebAdminServer"])
    if not web_admin_module.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    plugin = SimpleNamespace(
        config={"web_admin": {"enabled": True, "password": "first-password"}},
        version="test",
        session_data={},
        scheduler=None,
        group_timers={},
        auto_trigger_timers={},
        notification_center=None,
    )
    server = web_admin_module.WebAdminServer(plugin)
    websocket = SimpleNamespace(close=AsyncMock(), send_json=AsyncMock())
    server._ws_connections = [websocket]
    plugin.config["web_admin"]["password"] = "rotated-out-of-band"

    await server._broadcast_ws_payload({"type": "update"})

    websocket.close.assert_awaited_once_with(code=1008)
    websocket.send_json.assert_not_awaited()
    assert server._ws_connections == []


def test_websocket_rejects_non_auth_first_frame_before_broadcast() -> None:
    server, client, _token = _make_websocket_test_server()

    with pytest.raises(Exception):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ping"})
            ws.receive_json()

    assert server._ws_connections == []
