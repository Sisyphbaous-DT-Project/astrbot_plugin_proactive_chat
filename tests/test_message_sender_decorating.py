from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402
from astrbot.core.platform.platform import PlatformStatus  # noqa: E402

import core.message_sender as message_sender  # noqa: E402
from core.message_sender import SenderMixin, _SendOutcome  # noqa: E402
from core.session_parser import SessionMixin  # noqa: E402


class FakeMeta:
    def __init__(self, platform_id: str, name: str = "aiocqhttp") -> None:
        self.id = platform_id
        self.name = name


class FakePlatform:
    def __init__(
        self,
        platform_id: str,
        *,
        name: str = "aiocqhttp",
        failures: list[bool] | None = None,
    ) -> None:
        self._meta = FakeMeta(platform_id, name)
        self.status = PlatformStatus.RUNNING
        self.failures = list(failures or [])
        self.sent: list[tuple[object, MessageChain]] = []

    def meta(self) -> FakeMeta:
        return self._meta

    async def send_by_session(self, session, chain: MessageChain) -> None:
        self.sent.append((session, chain))
        if self.failures and self.failures.pop(0):
            raise RuntimeError(f"fake send failed: {self._meta.id}")


class FakePlatformManager:
    def __init__(self, platforms: list[FakePlatform]) -> None:
        self.platform_insts = platforms

    def get_insts(self) -> list[FakePlatform]:
        return self.platform_insts


class SenderHarness(SenderMixin):
    def __init__(
        self,
        platforms: list[FakePlatform],
        *,
        data_dir: Path | None = None,
    ) -> None:
        self.context = SimpleNamespace(
            platform_manager=FakePlatformManager(platforms),
            message_history_manager=None,
        )
        self.session_data: dict = {}
        self.data_dir = data_dir or Path.cwd()
        self.history_calls: list[MessageChain] = []
        self.cache_calls: list[str] = []
        self.reset_group_silence_timer = AsyncMock()
        self._session_configs: dict[str, dict] = {}

    def _parse_session_id(self, session_id: str):
        parts = session_id.split(":", 2)
        return tuple(parts) if len(parts) == 3 else None

    def _normalize_session_id(self, session_id: str) -> str:
        return session_id

    def _get_session_log_str(self, session_id: str, session_config=None) -> str:
        del session_config
        return session_id

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

    async def _persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        del session_id
        self.history_calls.append(chain)

    async def _cache_runtime_bot_message_direct(
        self,
        *,
        session_id: str,
        text: str,
        source: str,
    ) -> bool:
        del source
        self.cache_calls.append(f"{session_id}:{text}")
        return True

    async def _reset_group_silence_timer(self, session_id: str) -> None:
        await self.reset_group_silence_timer(session_id)

    async def _calc_interval(self, text: str, settings: dict) -> float:
        del text, settings
        return 0


class ProductionLogSender(SessionMixin, SenderHarness):
    """发送逻辑使用真实会话日志格式，其余边界沿用最小测试替身。"""


class FakeHandlerRegistry:
    def __init__(self, handlers) -> None:
        self.handlers = handlers

    def get_handlers_by_event_type(self, event_type):
        del event_type
        return self.handlers


def _handler(func, name: str = "fake_handler"):
    return SimpleNamespace(
        handler_full_name=f"tests.{name}",
        handler_module_path="tests.message_sender",
        handler=func,
    )


def _plain_text(chain: MessageChain) -> str:
    return "".join(
        component.text for component in chain.chain if isinstance(component, Plain)
    )


def test_tts_file_uri_is_cleaned(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio file.wav"
    audio_path.write_bytes(b"audio")

    SenderMixin._cleanup_tts_audio_file(audio_path.as_uri())

    assert audio_path.exists() is False


def test_tts_uppercase_localhost_file_uri_is_cleaned(tmp_path: Path) -> None:
    audio_path = tmp_path / "uppercase localhost.wav"
    audio_path.write_bytes(b"audio")
    uri = audio_path.as_uri().replace("file://", "file://LOCALHOST")

    SenderMixin._cleanup_tts_audio_file(uri)

    assert audio_path.exists() is False


@pytest.mark.asyncio
async def test_outputpro_style_early_send_is_really_delivered(monkeypatch) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    handler_calls = 0

    async def outputpro_style_handler(event) -> None:
        nonlocal handler_calls
        handler_calls += 1
        result = event.get_result()
        assert result is not None
        assert result.is_llm_result()
        assert event.get_extra("action_type") == "proactive"

        await event.send(MessageChain([Plain("第一段")]))
        result.chain[:] = [Plain("第二段")]

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(outputpro_style_handler, "split")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始完整回复")],
    )

    assert handler_calls == 1
    assert outcome.attempted_count == 2
    assert outcome.delivered_count == 2
    assert outcome.failed_count == 0
    assert [_plain_text(chain) for _, chain in platform.sent] == ["第一段", "第二段"]
    assert [_plain_text(chain) for chain in sender.history_calls] == [
        "第一段",
        "第二段",
    ]


@pytest.mark.asyncio
async def test_early_send_marks_operation_and_inherits_only_missing_metadata(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def outputpro_style_handler(event) -> None:
        assert event._has_send_oper is False

        await event.send(MessageChain([]))
        assert event._has_send_oper is True

        await event.send(MessageChain([Plain("继承默认标签")]))
        assert event._has_send_oper is True

        explicit = MessageChain([Plain("保留显式标签")])
        explicit.use_t2i_ = False
        explicit.type = "decorator-explicit"
        if hasattr(explicit, "use_markdown_"):
            explicit.use_markdown_ = False
        await event.send(explicit)

        result = event.get_result()
        assert result is not None
        result.chain[:] = [Plain("末段")]

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(outputpro_style_handler, "metadata")]),
    )

    source = MessageChain([Plain("原始完整回复")])
    source.use_t2i_ = True
    source.type = "source-type"
    if hasattr(source, "use_markdown_"):
        source.use_markdown_ = True

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        source,
    )

    assert outcome.attempted_count == 3
    assert outcome.delivered_count == 3
    assert outcome.failed_count == 0
    inherited = platform.sent[0][1]
    explicit = platform.sent[1][1]
    final = platform.sent[2][1]
    assert (inherited.use_t2i_, inherited.type) == (True, "source-type")
    assert (explicit.use_t2i_, explicit.type) == (False, "decorator-explicit")
    assert (final.use_t2i_, final.type) == (True, "source-type")
    if hasattr(source, "use_markdown_"):
        assert inherited.use_markdown_ is True
        assert explicit.use_markdown_ is False
        assert final.use_markdown_ is True


@pytest.mark.asyncio
async def test_stop_prevents_later_handlers_and_final_send(monkeypatch) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    second_calls = 0

    async def first_handler(event) -> None:
        event.stop_event()

    async def second_handler(event) -> None:
        nonlocal second_calls
        second_calls += 1

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry(
            [
                _handler(first_handler, "stop"),
                _handler(second_handler, "after_stop"),
            ]
        ),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("不会发送")],
    )

    assert outcome.stopped is True
    assert outcome.suppressed is True
    assert second_calls == 0
    assert platform.sent == []


@pytest.mark.asyncio
async def test_early_send_then_stop_counts_as_delivered(monkeypatch) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def handler(event) -> None:
        await event.send(MessageChain([Plain("已提前发送")]))
        event.stop_event()

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "send_then_stop")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:GroupMessage:20001",
        [Plain("末段不应重复发送")],
    )

    assert outcome.any_delivered is True
    assert outcome.stopped is True
    assert [_plain_text(chain) for _, chain in platform.sent] == ["已提前发送"]


@pytest.mark.asyncio
async def test_early_send_then_handler_exception_does_not_send_original(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def handler(event) -> None:
        await event.send(MessageChain([Plain("提前段")]))
        raise RuntimeError("后续装饰器异常")

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "send_then_error")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始完整回复")],
    )

    assert outcome.any_delivered is True
    assert outcome.stopped is True
    assert [_plain_text(chain) for _, chain in platform.sent] == ["提前段"]


@pytest.mark.asyncio
async def test_handler_exception_before_delivery_keeps_original_fallback(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def handler(event) -> None:
        raise RuntimeError("装饰器尚未发送就异常")

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "error_before_send")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始完整回复")],
    )

    assert outcome.any_delivered is True
    assert [_plain_text(chain) for _, chain in platform.sent] == ["原始完整回复"]


@pytest.mark.asyncio
async def test_platform_error_exposed_to_decorator_is_fixed_and_not_original_text(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    secret = "CHAT_SECRET_PLATFORM_FAILURE"

    async def direct_send(_session_id: str, _chain: MessageChain) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(sender, "_send_chain_direct", direct_send)
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    hook_result = await sender._trigger_decorating_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始完整回复")],
    )
    assert hook_result.event is not None

    with pytest.raises(RuntimeError) as raised:
        await hook_result.event.send(MessageChain([Plain("提前段")]))

    assert secret not in str(raised.value)
    assert hook_result.outcome.errors
    assert secret in str(hook_result.outcome.errors[0])


@pytest.mark.asyncio
async def test_decorator_exception_class_name_cannot_leak_into_logs(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    secret = "CHAT_SECRET_EXCEPTION_CLASS_LOG"
    dynamic_error_type = type(secret, (Exception,), {})

    class CaptureLogger:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def debug(self, message) -> None:
            self.messages.append(str(message))

        info = debug
        warning = debug
        error = debug

    capture = CaptureLogger()

    async def handler(event) -> None:
        del event
        raise dynamic_error_type("聊天正文")

    monkeypatch.setattr(message_sender, "logger", capture)
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "dynamic_exception_name")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始完整回复")],
    )

    assert outcome.any_delivered is True
    assert capture.messages
    assert secret not in repr(capture.messages)


@pytest.mark.asyncio
async def test_clear_result_suppresses_original_chain(monkeypatch) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def handler(event) -> None:
        event.clear_result()

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "clear")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("被拦截")],
    )

    assert outcome.suppressed is True
    assert outcome.any_delivered is False
    assert platform.sent == []


@pytest.mark.asyncio
async def test_blank_plain_result_is_suppressed_without_cache_or_history(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def handler(event) -> None:
        result = event.get_result()
        assert result is not None
        result.chain[:] = [Plain("   ")]

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "blank_block")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("原始消息")],
    )

    assert outcome.suppressed is True
    assert outcome.attempted_count == 0
    assert outcome.delivered_count == 0
    assert platform.sent == []
    assert sender.history_calls == []


@pytest.mark.asyncio
async def test_blank_outputpro_block_does_not_mark_proactive_message_sent(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    session_id = "qq-main:GroupMessage:20001"
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "group",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }

    async def block_handler(event) -> None:
        result = event.get_result()
        assert result is not None
        result.chain[:] = [Plain("")]

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(block_handler, "blank_block_integration")]),
    )

    sent = await sender._send_proactive_message(session_id, "不应进入缓存")

    assert sent is False
    assert sender.cache_calls == []
    sender.reset_group_silence_timer.assert_not_awaited()
    assert platform.sent == []


@pytest.mark.asyncio
async def test_false_send_by_session_is_counted_as_failure(monkeypatch) -> None:
    platform = FakePlatform("qq-main")

    async def false_send(session, chain):
        del session, chain
        return False

    platform.send_by_session = false_send
    sender = SenderHarness([platform])
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("发送失败")],
    )

    assert outcome.attempted_count == 1
    assert outcome.delivered_count == 0
    assert outcome.failed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("return_value", [None, False, True])
async def test_context_send_result_only_explicit_false_is_failure(
    monkeypatch,
    return_value,
) -> None:
    sender = SenderHarness([])
    sender.context.send_message = AsyncMock(return_value=return_value)
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "unparseable-session",
        [Plain("核心 API 发送")],
    )

    assert outcome.attempted_count == 1
    if return_value is False:
        assert outcome.delivered_count == 0
        assert outcome.failed_count == 1
        assert sender.history_calls == []
    else:
        assert outcome.delivered_count == 1
        assert outcome.failed_count == 0
        assert len(sender.history_calls) == 1


@pytest.mark.asyncio
async def test_cancelled_platform_history_write_does_not_undo_delivery(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])

    async def cancelled_history(session_id, chain):
        del session_id, chain
        raise asyncio.CancelledError

    sender._persist_proactive_message_to_platform_history = cancelled_history
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("物理发送已完成")],
    )

    assert outcome.any_delivered is True
    assert outcome.failed_count == 0
    assert len(platform.sent) == 1


@pytest.mark.asyncio
async def test_private_session_with_group_in_text_does_not_reset_group_timer(
    monkeypatch,
) -> None:
    sender = SenderHarness([FakePlatform("qq-main")])
    session_id = "qq-main:FriendMessage:group-10001"
    sender._send_chain_with_hooks = AsyncMock(
        return_value=_SendOutcome(attempted_count=1, delivered_count=1)
    )

    await sender._send_proactive_message(session_id, "私聊消息")

    sender.reset_group_silence_timer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "expected_delivered", "expected_failed"),
    [
        ([True, False], 1, 1),
        ([False, True], 1, 1),
        ([True, True], 0, 2),
    ],
)
async def test_partial_send_outcome_avoids_duplicate_retry(
    monkeypatch,
    failures: list[bool],
    expected_delivered: int,
    expected_failed: int,
) -> None:
    platform = FakePlatform("qq-main", failures=failures)
    sender = SenderHarness([platform])

    async def outputpro_style_handler(event) -> None:
        try:
            await event.send(MessageChain([Plain("提前段")]))
        except RuntimeError:
            pass
        result = event.get_result()
        assert result is not None
        result.chain[:] = [Plain("末段")]

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(outputpro_style_handler, "partial")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("完整消息")],
    )

    assert outcome.delivered_count == expected_delivered
    assert outcome.failed_count == expected_failed
    assert outcome.any_delivered is (expected_delivered > 0)
    assert outcome.partial is (expected_delivered > 0 and expected_failed > 0)


@pytest.mark.asyncio
async def test_exact_platform_id_wins_when_platform_names_are_same(monkeypatch) -> None:
    first = FakePlatform("qq-one")
    second = FakePlatform("qq-two")
    sender = SenderHarness([first, second])
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-two:FriendMessage:10001",
        [Plain("发给第二个实例")],
    )

    assert outcome.any_delivered is True
    assert first.sent == []
    assert [_plain_text(chain) for _, chain in second.sent] == ["发给第二个实例"]


@pytest.mark.asyncio
async def test_duplicate_platform_id_never_falls_back_to_first_instance(
    monkeypatch,
) -> None:
    first = FakePlatform("qq-duplicate")
    second = FakePlatform("qq-duplicate")
    sender = SenderHarness([first, second])
    sender.context.send_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-duplicate:FriendMessage:10001",
        [Plain("重复实例不应发送")],
    )

    assert outcome.any_delivered is False
    assert outcome.failed_count == 1
    sender.context.send_message.assert_not_awaited()
    assert first.sent == []
    assert second.sent == []


@pytest.mark.asyncio
async def test_ambiguous_platform_name_and_false_core_fallback_fail(
    monkeypatch,
) -> None:
    first = FakePlatform("qq-one")
    second = FakePlatform("qq-two")
    sender = SenderHarness([first, second])
    sender.context.send_message = AsyncMock(return_value=False)
    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([]),
    )

    outcome = await sender._send_chain_with_hooks(
        "aiocqhttp:FriendMessage:10001",
        [Plain("平台名称不唯一")],
    )

    assert outcome.any_delivered is False
    assert outcome.failed_count == 1
    assert first.sent == []
    assert second.sent == []


@pytest.mark.asyncio
async def test_decorator_temp_file_is_cleaned_after_final_send(
    monkeypatch,
    tmp_path: Path,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform], data_dir=tmp_path)
    temp_file = tmp_path / "decorator-temp.txt"
    temp_file.write_text("temporary", encoding="utf-8")

    async def handler(event) -> None:
        event.track_temporary_local_file(str(temp_file))

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "temp_file")]),
    )

    outcome = await sender._send_chain_with_hooks(
        "qq-main:FriendMessage:10001",
        [Plain("发送并清理")],
    )

    assert outcome.any_delivered is True
    assert not temp_file.exists()


@pytest.mark.asyncio
async def test_decorator_cancellation_propagates_after_temp_file_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform], data_dir=tmp_path)
    temp_file = tmp_path / "cancelled-decorator-temp.txt"
    temp_file.write_text("temporary", encoding="utf-8")

    async def handler(event) -> None:
        event.track_temporary_local_file(str(temp_file))
        raise asyncio.CancelledError

    monkeypatch.setattr(
        message_sender,
        "star_handlers_registry",
        FakeHandlerRegistry([_handler(handler, "cancelled")]),
    )

    with pytest.raises(asyncio.CancelledError):
        await sender._send_chain_with_hooks(
            "qq-main:FriendMessage:10001",
            [Plain("取消后不应发送")],
        )

    assert not temp_file.exists()
    assert platform.sent == []


@pytest.mark.asyncio
async def test_proactive_aggregation_caches_once_after_partial_success(
    monkeypatch,
) -> None:
    sender = SenderHarness([FakePlatform("qq-main")])
    session_id = "qq-main:GroupMessage:20001"
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "group",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {
            "enable": True,
            "words_count_threshold": 100,
            "regex": r"[^。]+。|.+$",
        },
    }
    sender._split_text = lambda text, settings: ["第一段", "第二段"]
    sender._send_chain_with_hooks = AsyncMock(
        side_effect=[
            _SendOutcome(attempted_count=1, delivered_count=1),
            _SendOutcome(attempted_count=1, failed_count=1),
        ]
    )
    monkeypatch.setattr(
        message_sender, "star_handlers_registry", FakeHandlerRegistry([])
    )

    sent = await sender._send_proactive_message(session_id, "第一段第二段")

    assert sent is True
    assert sender.cache_calls == [f"{session_id}:第一段第二段"]
    sender.reset_group_silence_timer.assert_awaited_once_with(session_id)


@pytest.mark.asyncio
async def test_proactive_suppression_does_not_cache_or_reset_group_timer() -> None:
    sender = SenderHarness([FakePlatform("qq-main")])
    session_id = "qq-main:GroupMessage:20001"
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "group",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }
    sender._send_chain_with_hooks = AsyncMock(
        return_value=_SendOutcome(suppressed=True)
    )

    sent = await sender._send_proactive_message(session_id, "被拦截消息")

    assert sent is False
    assert sender.cache_calls == []
    sender.reset_group_silence_timer.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_send_tolerates_legacy_bad_message_settings(
    monkeypatch,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform])
    session_id = "qq-main:FriendMessage:10001"
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": "bad",
        "segmented_reply_settings": "bad",
    }
    monkeypatch.setattr(
        message_sender, "star_handlers_registry", FakeHandlerRegistry([])
    )

    sent = await sender._send_proactive_message(session_id, "仍然可以发送")

    assert sent is True
    assert [_plain_text(chain) for _, chain in platform.sent] == ["仍然可以发送"]


@pytest.mark.asyncio
async def test_proactive_send_logs_do_not_include_session_identifier(
    monkeypatch,
) -> None:
    secret = "CHAT_SECRET_SESSION_4f8a"
    platform = FakePlatform("qq-main")
    sender = ProductionLogSender([platform])
    session_id = f"qq-main:FriendMessage:{secret}"
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": {"enable_tts": False, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }
    messages: list[str] = []
    capture = SimpleNamespace(
        debug=lambda *items, **_kwargs: messages.append(" ".join(map(str, items))),
        info=lambda *items, **_kwargs: messages.append(" ".join(map(str, items))),
        warning=lambda *items, **_kwargs: messages.append(" ".join(map(str, items))),
        error=lambda *items, **_kwargs: messages.append(" ".join(map(str, items))),
    )
    monkeypatch.setattr(message_sender, "logger", capture)
    monkeypatch.setattr(
        message_sender, "star_handlers_registry", FakeHandlerRegistry([])
    )

    sent = await sender._send_proactive_message(session_id, "正常正文")

    assert sent is True
    assert secret not in repr(messages)
    assert any("私聊会话" in message for message in messages)


@pytest.mark.asyncio
async def test_tts_temporary_audio_is_cleaned_after_send(
    monkeypatch,
    tmp_path: Path,
) -> None:
    platform = FakePlatform("qq-main")
    sender = SenderHarness([platform], data_dir=tmp_path)
    session_id = "qq-main:FriendMessage:10001"
    audio_file = tmp_path / "tts.wav"
    audio_file.write_bytes(b"fake-audio")

    class TtsProvider:
        async def get_audio(self, text: str) -> str:
            assert text == "语音消息"
            return str(audio_file)

    sender.context.get_using_tts_provider = lambda umo=None: TtsProvider()
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": {"enable_tts": True, "always_send_text": False},
        "segmented_reply_settings": {"enable": False},
    }
    monkeypatch.setattr(
        message_sender, "star_handlers_registry", FakeHandlerRegistry([])
    )

    sent = await sender._send_proactive_message(session_id, "语音消息")

    assert sent is True
    assert not audio_file.exists()
    assert len(platform.sent) == 1


@pytest.mark.asyncio
async def test_tts_temporary_audio_is_cleaned_when_send_is_cancelled(
    tmp_path: Path,
) -> None:
    sender = SenderHarness([FakePlatform("qq-main")], data_dir=tmp_path)
    session_id = "qq-main:FriendMessage:10001"
    audio_file = tmp_path / "tts-cancel.wav"
    audio_file.write_bytes(b"fake-audio")

    class TtsProvider:
        async def get_audio(self, text: str) -> str:
            del text
            return str(audio_file)

    async def cancelled_send(*_args, **_kwargs):
        raise asyncio.CancelledError

    sender.context.get_using_tts_provider = lambda umo=None: TtsProvider()
    sender._send_chain_with_hooks = cancelled_send
    sender._session_configs[session_id] = {
        "enable": True,
        "_session_type": "private",
        "tts_settings": {"enable_tts": True, "always_send_text": False},
        "segmented_reply_settings": {"enable": False},
    }

    with pytest.raises(asyncio.CancelledError):
        await sender._send_proactive_message(session_id, "语音消息")

    assert not audio_file.exists()
