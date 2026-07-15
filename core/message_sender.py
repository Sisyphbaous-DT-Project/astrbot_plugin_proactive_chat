"""发送与装饰钩子模块。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.star.star_handler import EventType, star_handlers_registry

try:
    from ..utils.safe_logging import exception_type_name, log_safe_exception
except ImportError:  # 允许测试直接以 core 包导入模块
    from utils.safe_logging import exception_type_name, log_safe_exception

try:
    from astrbot.api.event import AstrMessageEvent as AstrBotMessageEvent
except ImportError:
    AstrBotMessageEvent = None

try:
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:
    from astrbot.core.platform.message_session import MessageSession as MS

try:
    from astrbot.core.platform.sources.webchat.message_parts_helper import (
        message_chain_to_storage_message_parts,
    )
except ImportError:
    message_chain_to_storage_message_parts = None


def _message_chain_has_content(message: MessageChain | None) -> bool:
    """判断消息链是否包含平台真正可以发送的内容。"""
    if message is None:
        return False

    for component in getattr(message, "chain", ()) or ():
        if isinstance(component, Plain):
            if str(getattr(component, "text", "") or "").strip():
                return True
            continue
        # 图片、语音、At 等非文本组件即使没有可见文字，也是真实消息。
        if component is not None:
            return True
    return False


@dataclass
class _SendOutcome:
    """记录一次主动消息发送流程的物理发送结果。"""

    attempted_count: int = 0
    delivered_count: int = 0
    failed_count: int = 0
    stopped: bool = False
    suppressed: bool = False
    errors: list[BaseException] = field(default_factory=list, repr=False)

    @property
    def any_delivered(self) -> bool:
        """是否至少有一个物理消息成功送达。"""
        return self.delivered_count > 0

    @property
    def partial(self) -> bool:
        """是否存在成功和失败并存的部分成功。"""
        return self.delivered_count > 0 and self.failed_count > 0

    def merge(self, other: "_SendOutcome") -> "_SendOutcome":
        """合并另一次发送结果。"""
        self.attempted_count += other.attempted_count
        self.delivered_count += other.delivered_count
        self.failed_count += other.failed_count
        self.stopped = self.stopped or other.stopped
        self.suppressed = self.suppressed or other.suppressed
        self.errors.extend(other.errors)
        return self


@dataclass
class _DecoratingHookResult:
    """装饰钩子执行结果及其合成事件。"""

    event: Any | None
    result: MessageEventResult | None
    outcome: _SendOutcome


class _ProactiveSendError(RuntimeError):
    """向第三方装饰器暴露的固定发送错误。

    真实平台异常仍保存在 ``_SendOutcome.errors`` 中，供主动消息自己的
    日志和内部失败记录使用；这里不把第三方异常对象直接抛给装饰器，避免装饰器
    用 ``str(error)`` 把聊天正文或平台返回原文写入日志。
    """

    def __init__(self) -> None:
        super().__init__("主动消息平台发送失败")


if AstrBotMessageEvent is not None:

    class _ProactiveDecoratingEvent(AstrBotMessageEvent):
        """为主动消息装饰器提供真实发送能力的合成事件。"""

        def __init__(
            self,
            *args: Any,
            direct_send: Callable[[MessageChain], Awaitable[Any]],
            outcome: _SendOutcome,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._proactive_direct_send = direct_send
            self._proactive_outcome = outcome
            # AstrBot 4.8 的基础事件没有独立的 sticky stop 标志。
            self._proactive_force_stopped = False
            self._proactive_temporary_local_files: list[str] = []

        async def send(self, message: MessageChain) -> Any:
            """把装饰器的 event.send() 委托到主动消息直发边界。"""
            if not isinstance(message, MessageChain):
                raise TypeError("主动消息装饰事件只接受 MessageChain")

            # 与 AstrBot 基础事件的 send() 契约保持一致：调用过发送接口就
            # 记录该状态；空链仍不计入物理发送统计，也不会触达平台。
            self._has_send_oper = True
            if not _message_chain_has_content(message):
                return None

            outbound_message = MessageChain(chain=list(message.chain))
            for attr in ("use_t2i_", "type", "use_markdown_"):
                if hasattr(message, attr) and hasattr(outbound_message, attr):
                    setattr(outbound_message, attr, getattr(message, attr))

            # OutputPro 一类装饰器会为提前分段新建 MessageChain。仅为缺省值
            # 继承当前结果的元数据，避免覆盖装饰器显式指定的 False 或自定义类型。
            current_result = self.get_result()
            if current_result is not None:
                for attr in ("use_t2i_", "type", "use_markdown_"):
                    if not hasattr(outbound_message, attr) or not hasattr(
                        current_result, attr
                    ):
                        continue
                    if getattr(outbound_message, attr) is None:
                        inherited = getattr(current_result, attr)
                        if inherited is not None:
                            setattr(outbound_message, attr, inherited)

            self._proactive_outcome.attempted_count += 1
            try:
                result = await self._proactive_direct_send(outbound_message)
            except BaseException as error:
                self._proactive_outcome.failed_count += 1
                self._proactive_outcome.errors.append(error)
                # 取消和进程级控制信号不能被吞掉；普通平台异常则换成固定
                # 错误再交给装饰器，防止第三方读取异常正文或动态类型名。
                if isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                ):
                    raise
                raise _ProactiveSendError() from None

            self._proactive_outcome.delivered_count += 1
            return result

        def stop_event(self) -> None:
            self._proactive_force_stopped = True
            super().stop_event()

        def continue_event(self) -> None:
            self._proactive_force_stopped = False
            super().continue_event()

        def is_stopped(self) -> bool:
            return self._proactive_force_stopped or super().is_stopped()

        def track_temporary_local_file(self, path: str) -> None:
            """为没有该接口的旧版 AstrBot 提供临时文件登记。"""
            if path and path not in self._proactive_temporary_local_files:
                self._proactive_temporary_local_files.append(path)

            base_track = getattr(super(), "track_temporary_local_file", None)
            if callable(base_track):
                base_track(path)

        def cleanup_temporary_local_files(self) -> None:
            """清理合成事件及基础事件登记的临时文件。"""
            base_cleanup = getattr(super(), "cleanup_temporary_local_files", None)
            if callable(base_cleanup):
                base_cleanup()

            paths = list(self._proactive_temporary_local_files)
            self._proactive_temporary_local_files.clear()
            for path in paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as error:
                    log_safe_exception(
                        logger,
                        "warning",
                        "PC-SEND-002",
                        "清理装饰器临时文件失败",
                        error,
                    )

else:
    _ProactiveDecoratingEvent = None


class SenderMixin:
    """发送与装饰钩子混入类。"""

    context: Any
    session_data: dict
    data_dir: Any

    def _split_text(self, text: str, settings: dict) -> list[str]:
        """根据配置对文本进行分段。"""
        split_mode = settings.get("split_mode", "regex")

        # 新版 AstrBot（如 v4.20.1+）中，分段正则本身不再承担“匹配后自动移除命中字符”的旧行为。
        # 因此这里显式增加一个独立的内容清理阶段：
        # 1. 先按 split_mode 执行“切段”；
        # 2. 再在每个切好的分段上按 content_cleanup_rule 做二次清理。
        # 这样可以与官方的 segmented_reply.content_cleanup_rule 机制保持一致。
        enable_content_cleanup = settings.get("enable_content_cleanup", False)
        # 只有开关开启时才启用内容过滤规则；关闭时直接置空，确保完全保持旧版插件行为。
        content_cleanup_rule = (
            settings.get("content_cleanup_rule", "") if enable_content_cleanup else ""
        )
        content_cleanup_pattern: re.Pattern[str] | None = None
        if content_cleanup_rule:
            try:
                content_cleanup_pattern = re.compile(content_cleanup_rule)
            except re.error:
                logger.error(
                    "[主动消息] 内容清理正则表达式错误，将跳过内容清理并保留原始分段。"
                )

        if split_mode == "words":
            # words 模式下，先用分段词列表识别切分点。
            # 注意：这里的“切分”与“内容清理”是两件不同的事：
            # - split_words 负责决定在哪里断句；
            # - content_cleanup_rule 负责决定是否移除分段后的特定字符（如换行）。
            split_words = settings.get("split_words", ["。", "？", "！", "~", "…"])
            if not split_words:
                # 用户未提供分段词时退化为不分段，避免构造空正则导致行为不可预期。
                return [text]

            escaped_words = sorted(
                [re.escape(word) for word in split_words], key=len, reverse=True
            )
            # 保留分隔符，避免语气符号在切分时丢失
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)

            segments = pattern.findall(text)
            result: list[str] = []
            for seg in segments:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    if content_cleanup_pattern:
                        # 这里的 sub 属于“分段后清理”：
                        # content 已经是单个分段，不会再影响其他分段边界。
                        # 这样可避免把正则切分职责与内容删除职责耦合在一起。
                        content = content_cleanup_pattern.sub("", content)
                    if content.strip():
                        # 清理后若只剩空白，则直接丢弃，避免发送空消息段。
                        result.append(content)
                elif seg:
                    cleaned_seg = seg
                    if content_cleanup_pattern:
                        # 极少数情况下 findall 可能返回非 tuple 的字符串分段；
                        # 这里保持同样的清理策略，确保两类返回值行为一致。
                        cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
                    if cleaned_seg.strip():
                        result.append(cleaned_seg)
            return result if result else [text]

        # 正则分段模式
        # regex 仅用于“如何找出每一个分段”，不再假设其天然具备“删除命中字符”的副作用。
        # 若需要删除换行、句号等字符，应通过 content_cleanup_rule 明确声明。
        regex_pattern = settings.get("regex", r".*?[。？！~…\n]+|.+$")
        try:
            split_response = re.findall(regex_pattern, text, re.DOTALL | re.MULTILINE)
        except re.error:
            logger.error("[主动消息] 分段回复正则表达式错误，使用默认分段方式。")
            split_response = re.findall(
                r".*?[。？！~…\n]+|.+$", text, re.DOTALL | re.MULTILINE
            )

        result: list[str] = []
        for seg in split_response:
            cleaned_seg = seg
            if content_cleanup_pattern:
                # 与 words 模式保持一致：先完成切分，再对每段内容做独立清理。
                # 这样当默认规则为 [\n] 时，可稳定去除分段回复中残留的空行字符。
                cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
            if cleaned_seg.strip():
                # 过滤掉清理后为空的分段，避免平台收到空 Plain 消息。
                result.append(cleaned_seg)
        return result if result else [text]

    async def _calc_interval(self, text: str, settings: dict) -> float:
        """计算分段回复的间隔时间。"""
        interval_method = settings.get("interval_method", "random")

        # 对数间隔模式（模拟打字速度）
        if interval_method == "log":
            log_base = float(settings.get("log_base", 1.8))
            if all(ord(c) < 128 for c in text):
                word_count = len(text.split())
            else:
                word_count = len([c for c in text if c.isalnum()])
            i = math.log(word_count + 1, log_base)
            return random.uniform(i, i + 0.5)

        # 随机区间模式
        interval_str = settings.get("interval", "1.5, 3.5")
        try:
            interval_ls = [float(t) for t in interval_str.replace(" ", "").split(",")]
            interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
        except Exception:
            interval = [1.5, 3.5]

        return random.uniform(interval[0], interval[1])

    @staticmethod
    def _copy_message_chain_metadata(source: Any, target: Any) -> None:
        """在不同 AstrBot 版本之间复制消息链元数据。"""
        for attr in ("use_t2i_", "type", "use_markdown_"):
            if hasattr(source, attr) and hasattr(target, attr):
                setattr(target, attr, getattr(source, attr))

    def _coerce_message_chain(self, components: Any) -> MessageChain:
        """把列表或已有消息链统一成独立的 MessageChain。"""
        if isinstance(components, MessageChain):
            chain = MessageChain(chain=list(components.chain))
            self._copy_message_chain_metadata(components, chain)
            return chain

        return MessageChain(chain=list(components or []))

    def _get_platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []

        get_insts = getattr(manager, "get_insts", None)
        if callable(get_insts):
            return list(get_insts())
        return list(getattr(manager, "platform_insts", []) or [])

    def _resolve_target_platform(
        self,
        platform_id_or_name: str,
    ) -> tuple[Any | None, bool]:
        """按实例 ID 精确选平台，并返回是否存在歧义。"""
        platforms = self._get_platform_instances()
        exact_matches = [
            platform
            for platform in platforms
            if str(platform.meta().id) == str(platform_id_or_name)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], False
        if len(exact_matches) > 1:
            logger.error("[主动消息] 平台实例 ID 重复，拒绝发送以避免发错目标。")
            return None, True

        name_matches = [
            platform
            for platform in platforms
            if str(platform.meta().name) == str(platform_id_or_name)
        ]
        if len(name_matches) == 1:
            return name_matches[0], False
        if len(name_matches) > 1:
            logger.error("[主动消息] 平台名称对应多个实例，拒绝发送以避免发错目标。")
            return None, True
        return None, False

    @staticmethod
    def _message_type_from_session(message_type: str) -> MessageType:
        if message_type in {"GroupMessage", "GuildMessage"}:
            return MessageType.GROUP_MESSAGE
        return MessageType.FRIEND_MESSAGE

    def _track_sender_error(self, error: Exception, module: str) -> None:
        log_safe_exception(
            logger,
            "error",
            "PC-SEND-001",
            f"{module} 发送失败",
            error,
        )

    async def _send_chain_direct(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> Any:
        """只负责真实发送，不触发任何装饰钩子。"""
        try:
            parsed = self._parse_session_id(session_id)
            if not parsed:
                sent = await self.context.send_message(session_id, chain)
                if sent is False:
                    raise RuntimeError("AstrBot 核心 API 未找到可发送的平台")
                try:
                    await self._persist_proactive_message_to_platform_history(
                        session_id, chain
                    )
                except asyncio.CancelledError:
                    # 平台已经确认发送成功，历史补写被取消不能让本轮被误判成失败。
                    logger.warning("[主动消息] 平台流水补写被取消，保留已送达结果喵。")
                return sent

            platform_id, message_type, target_id = parsed
            target_platform, ambiguous = self._resolve_target_platform(platform_id)
            if target_platform is None:
                if ambiguous:
                    raise RuntimeError(f"平台标识不唯一，拒绝发送: {platform_id}")
                logger.warning(
                    "[主动消息] 找不到唯一的目标平台，尝试使用 AstrBot 核心 API 发送喵。"
                )
                sent = await self.context.send_message(session_id, chain)
                if sent is False:
                    raise RuntimeError(f"找不到可发送的平台: {platform_id}")
                try:
                    await self._persist_proactive_message_to_platform_history(
                        session_id, chain
                    )
                except asyncio.CancelledError:
                    logger.warning("[主动消息] 平台流水补写被取消，保留已送达结果喵。")
                return sent

            status = getattr(target_platform, "status", PlatformStatus.RUNNING)
            if status != PlatformStatus.RUNNING:
                raise RuntimeError(f"平台 {platform_id} 当前未运行: {status}")

            target_meta = target_platform.meta()
            session = MS(
                platform_name=target_meta.id,
                message_type=self._message_type_from_session(message_type),
                session_id=target_id,
            )
            sent = await target_platform.send_by_session(session, chain)
            if sent is False:
                raise RuntimeError(f"平台 {platform_id} 明确报告发送失败")
            logger.debug("[主动消息] 消息将通过目标平台送达喵。")

            if target_meta.id != "webchat":
                history_session_id = session_id
                if str(target_meta.id) != str(platform_id):
                    history_session_id = f"{target_meta.id}:{message_type}:{target_id}"
                try:
                    await self._persist_proactive_message_to_platform_history(
                        history_session_id, chain
                    )
                except asyncio.CancelledError:
                    # 物理发送已经完成；补写历史属于尽力而为，不能吞掉已送达结果。
                    logger.warning("[主动消息] 平台流水补写被取消，保留已送达结果喵。")
            return None
        except Exception as error:
            self._track_sender_error(
                error,
                module="core.message_sender._send_chain_direct",
            )
            raise

    async def _trigger_decorating_hooks(
        self,
        session_id: str,
        chain: list | MessageChain,
    ) -> _DecoratingHookResult:
        """触发装饰钩子并返回事件、结果链及物理发送统计。"""
        source_chain = self._coerce_message_chain(chain)
        outcome = _SendOutcome()
        result = MessageEventResult(chain=list(source_chain.chain))
        self._copy_message_chain_metadata(source_chain, result)
        result.set_result_content_type(ResultContentType.LLM_RESULT)

        parsed = self._parse_session_id(session_id)
        if not parsed or not AstrBotMessageEvent or not _ProactiveDecoratingEvent:
            return _DecoratingHookResult(None, result, outcome)

        platform_id, message_type, target_id = parsed
        platform_inst, _ambiguous = self._resolve_target_platform(platform_id)
        if platform_inst is None:
            return _DecoratingHookResult(None, result, outcome)

        message_obj = AstrBotMessage()
        message_obj.type = self._message_type_from_session(message_type)
        if message_obj.type == MessageType.GROUP_MESSAGE:
            message_obj.group = Group(group_id=target_id)

        message_obj.session_id = target_id
        message_obj.message = list(source_chain.chain)
        session_state = self.session_data.get(session_id, {})
        message_obj.self_id = session_state.get("self_id", "bot")
        message_obj.sender = MessageMember(user_id=target_id)
        message_obj.message_str = ""
        message_obj.raw_message = None
        message_obj.message_id = ""

        async def direct_send(message: MessageChain) -> Any:
            return await self._send_chain_direct(session_id, message)

        event = _ProactiveDecoratingEvent(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_inst.meta(),
            session_id=target_id,
            direct_send=direct_send,
            outcome=outcome,
        )
        event.set_extra("action_type", "proactive")
        event.set_result(result)

        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnDecoratingResultEvent
            )
            for handler in handlers:
                try:
                    logger.debug("[主动消息] 正在执行装饰钩子喵。")
                    await handler.handler(event)
                except Exception as error:
                    error_type = exception_type_name(error)
                    log_safe_exception(
                        logger,
                        "error",
                        "PC-SEND-003",
                        "执行装饰钩子失败",
                        error,
                    )
                    if "Available" in error_type:
                        logger.error(
                            "[主动消息] 捕获到可能导致 ApiNotAvailable 的装饰钩子喵。"
                        )
                    if outcome.any_delivered:
                        # 已经有提前分段真实送达时，不能再把完整原文作为兜底发出。
                        event.clear_result()
                        event.stop_event()
                        outcome.stopped = True
                        break

                if event.is_stopped():
                    outcome.stopped = True
                    break
        except BaseException:
            # 取消、退出等控制信号必须继续向上传播，但此时外层尚未拿到
            # hook_result，需在这里释放装饰器已经登记的临时文件。
            cleanup = getattr(event, "cleanup_temporary_local_files", None)
            if callable(cleanup):
                cleanup()
            raise

        result = event.get_result()
        if result is None or not _message_chain_has_content(result):
            outcome.suppressed = outcome.attempted_count == 0
        return _DecoratingHookResult(event, result, outcome)

    async def _persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        """将主动消息补写入平台消息流水，弥补部分适配器不会自动持久化的问题。"""
        try:
            parsed = self._parse_session_id(session_id)
        except Exception as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-SEND-004",
                "解析会话标识失败，跳过平台流水补写",
                e,
            )
            return

        if not parsed:
            return

        platform_id, _message_type, target_id = parsed
        history_mgr = getattr(self.context, "message_history_manager", None)
        if not history_mgr:
            return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(self.data_dir) / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            if message_chain_to_storage_message_parts is not None:
                message_parts = await message_chain_to_storage_message_parts(
                    chain,
                    insert_attachment=insert_attachment,
                    attachments_dir=attachments_dir,
                )
            else:
                # AstrBot 4.8 尚未提供 WebChat 的转换辅助函数，使用兼容的
                # 旧版存储格式补写文本和通用组件，避免流水静默丢失。
                message_parts = self._message_chain_to_legacy_storage_parts(chain)
            if not message_parts:
                return

            await history_mgr.insert(
                platform_id=platform_id,
                user_id=target_id,
                content={"type": "bot", "message": message_parts},
                sender_id="bot",
                sender_name="bot",
            )
            logger.debug("[主动消息] 已将主动消息补写入目标平台流水喵。")
        except Exception as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-SEND-005",
                "补写平台流水失败",
                e,
            )

    @staticmethod
    def _message_chain_to_legacy_storage_parts(chain: MessageChain) -> list[dict]:
        """把消息链转换成 AstrBot 4.8 使用的旧版平台流水格式。"""
        parts: list[dict] = []
        for component in getattr(chain, "chain", ()) or ():
            if isinstance(component, Plain):
                text = str(getattr(component, "text", "") or "")
                if text:
                    parts.append({"type": "plain", "text": text})
                continue

            try:
                raw = component.toDict()
            except (AttributeError, TypeError):
                raw = {"type": component.__class__.__name__.lower()}

            if not isinstance(raw, dict):
                continue
            part_type = str(raw.get("type") or "component").lower()
            data = raw.get("data")
            if isinstance(data, dict):
                part = {"type": part_type, **data}
            else:
                part = {"type": part_type}
            parts.append(part)
        return parts

    def _extract_history_content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text") or ""))
                    elif "text" in item:
                        parts.append(str(item.get("text") or ""))
                elif hasattr(item, "text"):
                    parts.append(str(getattr(item, "text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts).strip()
        return str(content or "").strip()

    def _build_sender_conversation_session_candidates(
        self, session_id: str
    ) -> list[str]:
        candidate_builder = getattr(
            self, "_build_conversation_session_candidates", None
        )
        if callable(candidate_builder):
            try:
                candidates = candidate_builder(session_id)
                if isinstance(candidates, list) and candidates:
                    return [str(item) for item in candidates if str(item or "").strip()]
            except Exception as e:
                log_safe_exception(
                    logger,
                    "debug",
                    "PC-SEND-006",
                    "构建对话候选 UMO 失败，使用发送侧兜底逻辑",
                    e,
                )

        candidates: list[str] = []

        def _append(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)

        try:
            normalized_session_id = self._normalize_session_id(session_id)
        except Exception:
            normalized_session_id = session_id

        session_data = getattr(self, "session_data", {})
        if isinstance(session_data, dict):
            for state_key in (normalized_session_id, session_id):
                state = session_data.get(state_key, {})
                if isinstance(state, dict):
                    _append(state.get("last_event_umo"))

        _append(session_id)
        _append(normalized_session_id)
        return candidates

    async def _new_conversation_for_history(
        self,
        conv_mgr: Any,
        session_id: str,
    ) -> str | None:
        platform_id = None
        try:
            parsed = self._parse_session_id(session_id)
            if parsed:
                platform_id = parsed[0]
        except Exception:
            platform_id = None

        try:
            return await conv_mgr.new_conversation(
                session_id,
                platform_id=platform_id,
            )
        except TypeError:
            return await conv_mgr.new_conversation(session_id)

    async def _get_conversation_for_history(
        self,
        conv_mgr: Any,
        session_id: str,
        conv_id: str,
    ) -> Any | None:
        try:
            return await conv_mgr.get_conversation(
                session_id,
                conv_id,
                create_if_not_exists=True,
            )
        except TypeError:
            conversation = await conv_mgr.get_conversation(session_id, conv_id)
            if conversation is None:
                new_conv_id = await self._new_conversation_for_history(
                    conv_mgr,
                    session_id,
                )
                if not new_conv_id:
                    return None
                return await conv_mgr.get_conversation(session_id, new_conv_id)

    async def _sync_sender_conversation_aliases(
        self,
        conv_id: str,
        session_ids: list[str],
    ) -> None:
        syncer = getattr(self, "_sync_conversation_aliases", None)
        if callable(syncer):
            try:
                await syncer(conv_id, session_ids)
                return
            except Exception as e:
                log_safe_exception(
                    logger,
                    "debug",
                    "PC-SEND-007",
                    "同步对话别名失败，使用发送侧兜底逻辑",
                    e,
                )

        conv_mgr = getattr(self.context, "conversation_manager", None)
        switcher = getattr(conv_mgr, "switch_conversation", None)
        if not callable(switcher):
            return

        for session_id in session_ids:
            try:
                await switcher(session_id, conv_id)
            except Exception as e:
                log_safe_exception(
                    logger,
                    "debug",
                    "PC-SEND-008",
                    "同步对话别名失败",
                    e,
                )

    async def _persist_proactive_message_to_conversation_history(
        self,
        session_id: str,
        text: str,
    ) -> bool:
        """Append the sent proactive text as an assistant turn in AstrBot history."""
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return False

        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            logger.debug(
                "[主动消息] 当前 AstrBot 上下文没有 conversation_manager，跳过对话历史写入喵。"
            )
            return False

        candidates = self._build_sender_conversation_session_candidates(session_id)
        if not candidates:
            return False

        conv_id = None
        effective_session_id = candidates[0]
        for candidate in candidates:
            try:
                candidate_conv_id = await conv_mgr.get_curr_conversation_id(candidate)
            except Exception as e:
                log_safe_exception(
                    logger,
                    "debug",
                    "PC-SEND-009",
                    "读取当前对话 ID 失败",
                    e,
                )
                continue
            if candidate_conv_id:
                conv_id = candidate_conv_id
                effective_session_id = candidate
                break

        try:
            if not conv_id:
                conv_id = await self._new_conversation_for_history(
                    conv_mgr,
                    effective_session_id,
                )

            if not conv_id:
                logger.warning(
                    "[主动消息] 无法获取或创建当前对话，跳过写入 LLM 对话历史。"
                )
                return False

            await self._sync_sender_conversation_aliases(conv_id, candidates)

            conversation = await self._get_conversation_for_history(
                conv_mgr,
                effective_session_id,
                conv_id,
            )
            if not conversation:
                logger.warning("[主动消息] 无法获取当前对话，跳过写入 LLM 对话历史。")
                return False

            raw_history = getattr(conversation, "history", None) or "[]"
            if isinstance(raw_history, str):
                try:
                    history = json.loads(raw_history)
                except json.JSONDecodeError:
                    logger.warning(
                        "[主动消息] 当前对话历史 JSON 解析失败，将从空历史开始追加。"
                    )
                    history = []
            elif isinstance(raw_history, list):
                history = raw_history
            else:
                history = []

            if not isinstance(history, list):
                history = []

            if history:
                last = history[-1]
                if (
                    isinstance(last, dict)
                    and last.get("role") == "assistant"
                    and self._extract_history_content_text(last.get("content"))
                    == normalized_text
                ):
                    logger.debug(
                        "[主动消息] 当前对话历史末尾已存在相同主动消息，跳过重复写入。"
                    )
                    return False

            history.append({"role": "assistant", "content": normalized_text})

            await conv_mgr.update_conversation(
                unified_msg_origin=effective_session_id,
                conversation_id=conv_id,
                history=history,
            )

            logger.info("[主动消息] 已将主动消息写入 AstrBot 当前 LLM 对话历史。")
            return True

        except Exception as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-SEND-010",
                "写入 AstrBot 当前 LLM 对话历史失败",
                e,
            )
            return False

    async def _persist_proactive_pair_to_conversation_history(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
    ) -> bool:
        """Append the proactive prompt and sent reply to the selected conversation."""
        assistant_text = str(assistant_response or "").strip()
        if not assistant_text:
            return False

        user_text = str(user_prompt or "").strip() or "[主动消息触发]"
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr or not conv_id:
            logger.warning(
                "[主动消息] 无法写入对话历史：conversation_manager 或 conv_id 为空。"
            )
            return False

        candidates = self._build_sender_conversation_session_candidates(session_id)
        effective_session_id = candidates[0] if candidates else session_id

        try:
            conversation = await self._get_conversation_for_history(
                conv_mgr,
                effective_session_id,
                conv_id,
            )
            if not conversation:
                logger.warning("[主动消息] 找不到目标对话，无法写入主动消息历史。")
                return False

            raw_history = getattr(conversation, "history", None) or "[]"
            if isinstance(raw_history, str):
                try:
                    history = json.loads(raw_history)
                except json.JSONDecodeError:
                    logger.warning(
                        "[主动消息] 当前对话历史 JSON 解析失败，将从空历史开始追加。"
                    )
                    history = []
            elif isinstance(raw_history, list):
                history = raw_history
            else:
                history = []

            if not isinstance(history, list):
                history = []

            # 先检查完整的 user/assistant 对，不能先删除末尾 assistant，
            # 否则第二次写入同一主动消息时会把本来可识别的完整 pair 拆散。
            if len(history) >= 2:
                last_user = history[-2]
                last_assistant = history[-1]
                if (
                    isinstance(last_user, dict)
                    and isinstance(last_assistant, dict)
                    and last_user.get("role") == "user"
                    and last_assistant.get("role") == "assistant"
                    and self._extract_history_content_text(last_user.get("content"))
                    == user_text
                    and self._extract_history_content_text(
                        last_assistant.get("content")
                    )
                    == assistant_text
                ):
                    logger.debug(
                        "[主动消息] 当前对话历史末尾已存在相同主动消息轮次，跳过重复写入。"
                    )
                    return False

            # 如果底层发送接口已经补写了相同的 assistant，再删掉这一个孤立
            # assistant，随后由本函数补上完整 pair，避免历史中出现重复回复。
            if (
                history
                and isinstance(history[-1], dict)
                and history[-1].get("role") == "assistant"
                and self._extract_history_content_text(history[-1].get("content"))
                == assistant_text
            ):
                history.pop()

            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": assistant_text})
            # 使用当前读取到的完整历史更新，确保上面为去重而删除的孤立
            # assistant 不会因为 add_message_pair 重新从数据库读回而复活。
            await conv_mgr.update_conversation(
                unified_msg_origin=effective_session_id,
                conversation_id=conv_id,
                history=history,
            )

            await self._sync_sender_conversation_aliases(conv_id, candidates)
            logger.info("[主动消息] 已写入主动消息到 AstrBot LLM 对话历史。")
            return True

        except Exception as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-SEND-011",
                "写入主动消息到 AstrBot LLM 对话历史失败",
                e,
            )
            return False

    @staticmethod
    def _coerce_send_outcome(value: Any) -> _SendOutcome:
        """兼容旧测试或外部替身返回的 bool 发送结果。"""
        if isinstance(value, _SendOutcome):
            return value

        outcome = _SendOutcome(attempted_count=1)
        if value:
            outcome.delivered_count = 1
        else:
            outcome.failed_count = 1
        return outcome

    async def _attempt_direct_send(
        self,
        session_id: str,
        chain: MessageChain,
        outcome: _SendOutcome,
    ) -> None:
        """在没有合成事件时执行一次可统计的直发。"""
        outcome.attempted_count += 1
        try:
            await self._send_chain_direct(session_id, chain)
        except Exception as error:
            outcome.failed_count += 1
            outcome.errors.append(error)
            return
        outcome.delivered_count += 1

    async def _send_chain_with_hooks(
        self,
        session_id: str,
        components: list | MessageChain,
    ) -> _SendOutcome:
        """发送消息链（含装饰钩子），返回物理发送统计。"""
        hook_result = await self._trigger_decorating_hooks(session_id, components)
        outcome = hook_result.outcome

        try:
            result = hook_result.result
            if hook_result.event is not None:
                event = hook_result.event
                if (
                    not outcome.stopped
                    and result is not None
                    and _message_chain_has_content(result)
                ):
                    final_chain = MessageChain(chain=list(result.chain))
                    self._copy_message_chain_metadata(result, final_chain)
                    try:
                        await event.send(final_chain)
                    except Exception:
                        # 发送异常已经由合成事件计入 outcome；保留后续调度决策。
                        pass
                elif not outcome.any_delivered and outcome.attempted_count == 0:
                    outcome.suppressed = True
            elif result is not None and _message_chain_has_content(result):
                final_chain = MessageChain(chain=list(result.chain))
                self._copy_message_chain_metadata(result, final_chain)
                await self._attempt_direct_send(session_id, final_chain, outcome)
            elif not outcome.any_delivered and outcome.attempted_count == 0:
                outcome.suppressed = True

            if outcome.partial:
                logger.warning(
                    f"[主动消息] 消息存在部分发送成功：已送达 {outcome.delivered_count} 段，"
                    f"失败 {outcome.failed_count} 段，不自动重发以避免重复消息喵。"
                )
            return outcome
        finally:
            cleanup = getattr(hook_result.event, "cleanup_temporary_local_files", None)
            if callable(cleanup):
                cleanup()

    async def _send_proactive_message(self, session_id: str, text: str) -> bool:
        """发送主动消息（支持TTS与分段）。"""
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(
                f"[主动消息] 无法获取会话配置，跳过 {self._get_session_log_str(session_id)} 的消息发送喵。"
            )
            return False

        logger.info(
            f"[主动消息] 开始发送 {self._get_session_log_str(session_id, session_config)} 的主动消息喵。"
        )

        tts_conf = session_config.get("tts_settings", {})
        if not isinstance(tts_conf, dict):
            tts_conf = {}
        seg_conf = session_config.get("segmented_reply_settings", {})
        if not isinstance(seg_conf, dict):
            seg_conf = {}

        # 先尝试 TTS：成功后是否继续发文本由 always_send_text 控制
        overall_outcome = _SendOutcome()
        is_tts_sent = False
        is_text_sent = False
        if tts_conf.get("enable_tts", True):
            audio_path: str | None = None
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        tts_result = self._coerce_send_outcome(
                            await self._send_chain_with_hooks(
                                session_id, [Record(file=audio_path)]
                            )
                        )
                        overall_outcome.merge(tts_result)
                        is_tts_sent = tts_result.any_delivered
                        await asyncio.sleep(0.5)
            except Exception as e:
                log_safe_exception(
                    logger,
                    "error",
                    "PC-SEND-012",
                    "手动 TTS 流程发生异常",
                    e,
                )
            finally:
                self._cleanup_tts_audio_file(audio_path)

        # 是否继续发送文本：未发出 TTS 或配置要求始终发文本
        should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)

        if should_send_text:
            enable_seg = seg_conf.get("enable", False)
            threshold = seg_conf.get("words_count_threshold", 150)

            # 注意：这里的 threshold 语义是“**不分段字数阈值**”，与字段名历史含义保持一致。
            # 也就是说：
            # 1. 文本较短（<= threshold）时，允许按规则切成多段，模拟更自然的连续输出；
            # 2. 文本较长（> threshold）时，直接整段发送，避免长文被切碎后影响阅读体验。
            # 该行为与 [`_conf_schema.json`](./_conf_schema.json) 和 [`README.md`](README.md) 的现有说明一致，
            # 因此这里不是“超过阈值才分段”的常见语义，而是本插件刻意保留的兼容策略。
            if enable_seg and len(text) <= threshold:
                segments = self._split_text(text, seg_conf)
                if not segments:
                    segments = [text]
                logger.info(
                    f"[主动消息] 分段回复已启用，将发送 {len(segments)} 条消息喵。"
                )

                # 分段顺序发送，段间按策略等待，模拟自然输出节奏
                for idx, seg in enumerate(segments):
                    segment_result = self._coerce_send_outcome(
                        await self._send_chain_with_hooks(
                            session_id,
                            [Plain(text=seg)],
                        )
                    )
                    overall_outcome.merge(segment_result)
                    is_text_sent = is_text_sent or segment_result.any_delivered
                    if idx < len(segments) - 1:
                        interval = await self._calc_interval(seg, seg_conf)
                        logger.debug(f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。")
                        await asyncio.sleep(interval)
            else:
                text_result = self._coerce_send_outcome(
                    await self._send_chain_with_hooks(
                        session_id,
                        [Plain(text=text)],
                    )
                )
                overall_outcome.merge(text_result)
                is_text_sent = text_result.any_delivered

        if overall_outcome.any_delivered:
            cached = await self._cache_runtime_bot_message_direct(
                session_id=session_id,
                text=text,
                source="proactive_send",
            )
            if cached:
                logger.info(
                    f"[主动消息] 已记录本次主动发送内容：{self._get_session_log_str(session_id, session_config)}。"
                )

        # Bot 在群聊发言后需要重置沉默计时。只看解析出的消息类型，不能
        # 在整个 UMO 文本里搜索 group/guild，否则平台名或目标 ID 含这些
        # 字符的私聊也会被误当成群聊。
        parse_session_id = getattr(self, "_parse_session_id", None)
        try:
            parsed_session = (
                parse_session_id(session_id) if callable(parse_session_id) else None
            )
        except Exception:
            parsed_session = None
        is_group_session = bool(
            parsed_session and parsed_session[1] in {"GroupMessage", "GuildMessage"}
        )
        if is_group_session:
            if not overall_outcome.any_delivered:
                return False
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {self._get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )

        return overall_outcome.any_delivered

    @staticmethod
    def _cleanup_tts_audio_file(audio_path: str | None) -> None:
        """清理 TTS Provider 生成的本地临时音频文件。"""
        if not audio_path:
            return
        text_path = str(audio_path)
        lower_path = text_path.lower()
        if (
            lower_path.startswith("http://")
            or lower_path.startswith("https://")
            or lower_path.startswith("base64://")
        ):
            return
        if lower_path.startswith("file://"):
            parsed = urlparse(text_path)
            # 仅清理本机 file URI；网络位置不属于本插件的临时文件范围。
            if parsed.scheme.lower() != "file" or parsed.netloc.casefold() not in {
                "",
                "localhost",
            }:
                return
            try:
                text_path = url2pathname(unquote(parsed.path))
            except (TypeError, ValueError):
                return
            if not text_path:
                return
        path = Path(text_path)
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                path.unlink()
