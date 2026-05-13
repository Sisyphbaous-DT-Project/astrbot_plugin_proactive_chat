"""发送与装饰钩子模块。"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import traceback
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.star.star_handler import EventType, star_handlers_registry

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


class SenderMixin:
    """发送与装饰钩子混入类。"""

    context: Any
    session_data: dict
    telemetry: Any
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
                    "[主动消息] 内容清理正则表达式错误，将跳过内容清理并保留原始分段: "
                    f"{traceback.format_exc()}"
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
            logger.error(
                f"[主动消息] 分段回复正则表达式错误，使用默认分段方式: {traceback.format_exc()}"
            )
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

    async def _trigger_decorating_hooks(self, session_id: str, chain: list) -> list:
        """触发 OnDecoratingResultEvent 钩子。"""
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return chain

        # 解析出平台、消息类型、目标 ID，用于构造事件上下文
        platform_name, msg_type_str, target_id = parsed
        platform_inst = None
        for p in self.context.platform_manager.platform_insts:
            if p.meta().id == platform_name:
                platform_inst = p
                break

        # 兼容按平台显示名匹配（部分平台可能用 name 进行标识）
        if not platform_inst:
            for p in self.context.platform_manager.platform_insts:
                if p.meta().name == platform_name:
                    platform_inst = p
                    break

        if not platform_inst:
            return chain

        # 构造伪造的消息对象以触发装饰链
        message_obj = AstrBotMessage()
        if "Friend" in msg_type_str:
            message_obj.type = MessageType.FRIEND_MESSAGE
        elif "Group" in msg_type_str:
            message_obj.type = MessageType.GROUP_MESSAGE
            message_obj.group = Group(group_id=target_id)
        else:
            message_obj.type = MessageType.FRIEND_MESSAGE

        # 构造最小可用消息对象，让装饰器可在统一事件结构上改写链
        message_obj.session_id = target_id
        message_obj.message = chain
        message_obj.self_id = self.session_data.get(session_id, {}).get(
            "self_id", "bot"
        )
        message_obj.sender = MessageMember(user_id=target_id)
        message_obj.message_str = ""
        message_obj.raw_message = None
        message_obj.message_id = ""

        # 旧版本若无事件类则跳过装饰阶段，直接返回原链
        if not AstrBotMessageEvent:
            return chain

        event = AstrBotMessageEvent(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_inst.meta(),
            session_id=target_id,
        )

        # 注入结果链以便装饰器修改
        res = MessageEventResult()
        res.chain = chain
        event.set_result(res)

        # 顺序执行所有 OnDecoratingResultEvent 处理器
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent
        )
        for handler in handlers:
            try:
                logger.debug(
                    f"[主动消息] 正在执行装饰钩子: {handler.handler_full_name} ({handler.handler_module_path}) 喵"
                )
                await handler.handler(event)
            except Exception as e:
                error_type = type(e).__name__
                logger.error(
                    f"[主动消息] 执行装饰钩子失败喵！来源: {handler.handler_full_name}, "
                    f"错误类型: {error_type}, 错误详情: {e}"
                )
                if self.telemetry and self.telemetry.enabled:
                    # 装饰钩子属于外围扩展链路，单独上报便于定位是否为第三方装饰器导致的问题。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_error(
                                e,
                                module="core.message_sender._trigger_decorating_hooks",
                            )
                        )
                    )
                if "Available" in error_type:
                    logger.error(
                        f"[主动消息] 抓到可能导致 ApiNotAvailable 的嫌疑人喵！模块: {handler.handler_module_path}"
                    )

        res = event.get_result()
        if res is not None:
            return res.chain if res.chain is not None else []
        return chain

    async def _persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        """将主动消息补写入平台消息流水，弥补部分适配器不会自动持久化的问题。"""
        try:
            parsed = self._parse_session_id(session_id)
        except Exception as e:
            logger.warning(
                f"[主动消息] 解析会话标识失败，跳过平台流水补写喵: {e}",
                exc_info=True,
            )
            return

        if not parsed:
            return

        platform_id, _message_type, target_id = parsed
        history_mgr = getattr(self.context, "message_history_manager", None)
        if not history_mgr or message_chain_to_storage_message_parts is None:
            return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(self.data_dir) / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            message_parts = await message_chain_to_storage_message_parts(
                chain,
                insert_attachment=insert_attachment,
                attachments_dir=attachments_dir,
            )
            if not message_parts:
                return

            await history_mgr.insert(
                platform_id=platform_id,
                user_id=target_id,
                content={"type": "bot", "message": message_parts},
                sender_id="bot",
                sender_name="bot",
            )
            logger.debug(
                f"[主动消息] 已将主动消息补写入平台 ({platform_id}) 的流水喵，会话标识为 {target_id}。"
            )
        except Exception as e:
            logger.warning(f"[主动消息] 补写平台流水失败喵: {e}", exc_info=True)

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

    def _build_sender_conversation_session_candidates(self, session_id: str) -> list[str]:
        candidate_builder = getattr(self, "_build_conversation_session_candidates", None)
        if callable(candidate_builder):
            try:
                candidates = candidate_builder(session_id)
                if isinstance(candidates, list) and candidates:
                    return [str(item) for item in candidates if str(item or "").strip()]
            except Exception as e:
                logger.debug(f"[主动消息] 构建对话候选 UMO 失败，使用发送侧兜底逻辑喵: {e}")

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
                logger.debug(f"[主动消息] 同步对话别名失败，使用发送侧兜底逻辑喵: {e}")

        conv_mgr = getattr(self.context, "conversation_manager", None)
        switcher = getattr(conv_mgr, "switch_conversation", None)
        if not callable(switcher):
            return

        for session_id in session_ids:
            try:
                await switcher(session_id, conv_id)
            except Exception as e:
                logger.debug(
                    f"[主动消息] 同步对话别名失败：{self._get_session_log_str(session_id)} -> {conv_id}，错误：{e}"
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
            logger.debug("[主动消息] 当前 AstrBot 上下文没有 conversation_manager，跳过对话历史写入喵。")
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
                logger.debug(f"[主动消息] 读取当前对话 ID 失败：{candidate}，错误：{e}")
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
                    f"[主动消息] 无法获取或创建当前对话，跳过写入 LLM 对话历史：{effective_session_id}"
                )
                return False

            await self._sync_sender_conversation_aliases(conv_id, candidates)

            conversation = await self._get_conversation_for_history(
                conv_mgr,
                effective_session_id,
                conv_id,
            )
            if not conversation:
                logger.warning(
                    f"[主动消息] 无法获取当前对话，跳过写入 LLM 对话历史：{effective_session_id}"
                )
                return False

            raw_history = getattr(conversation, "history", None) or "[]"
            if isinstance(raw_history, str):
                try:
                    history = json.loads(raw_history)
                except json.JSONDecodeError:
                    logger.warning(
                        f"[主动消息] 当前对话历史 JSON 解析失败，将从空历史开始追加：{effective_session_id}",
                        exc_info=True,
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
                        f"[主动消息] 当前对话历史末尾已存在相同主动消息，跳过重复写入：{effective_session_id}"
                    )
                    return False

            history.append({"role": "assistant", "content": normalized_text})

            await conv_mgr.update_conversation(
                unified_msg_origin=effective_session_id,
                conversation_id=conv_id,
                history=history,
            )

            logger.info(
                f"[主动消息] 已将主动消息写入 AstrBot 当前 LLM 对话历史：{effective_session_id}"
            )
            return True

        except Exception as e:
            logger.warning(
                f"[主动消息] 写入 AstrBot 当前 LLM 对话历史失败：{e}",
                exc_info=True,
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
            logger.warning("[主动消息] 无法写入对话历史：conversation_manager 或 conv_id 为空。")
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
                logger.warning(
                    f"[主动消息] 找不到目标对话，无法写入主动消息历史：session={effective_session_id}, conv_id={conv_id}"
                )
                return False

            raw_history = getattr(conversation, "history", None) or "[]"
            if isinstance(raw_history, str):
                try:
                    history = json.loads(raw_history)
                except json.JSONDecodeError:
                    logger.warning(
                        f"[主动消息] 当前对话历史 JSON 解析失败，将从空历史开始追加：{effective_session_id}",
                        exc_info=True,
                    )
                    history = []
            elif isinstance(raw_history, list):
                history = raw_history
            else:
                history = []

            if not isinstance(history, list):
                history = []

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
                        f"[主动消息] 当前对话历史末尾已存在相同主动消息轮次，跳过重复写入：{effective_session_id}"
                    )
                    return False

            add_pair = getattr(conv_mgr, "add_message_pair", None)
            if callable(add_pair):
                await add_pair(
                    cid=conv_id,
                    user_message={"role": "user", "content": user_text},
                    assistant_message={
                        "role": "assistant",
                        "content": assistant_text,
                    },
                )
            else:
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": assistant_text})
                await conv_mgr.update_conversation(
                    unified_msg_origin=effective_session_id,
                    conversation_id=conv_id,
                    history=history,
                )

            await self._sync_sender_conversation_aliases(conv_id, candidates)
            logger.info(
                f"[主动消息] 已写入主动消息到 AstrBot LLM 对话历史：session={effective_session_id}, conv_id={conv_id}"
            )
            return True

        except Exception as e:
            logger.warning(
                f"[主动消息] 写入主动消息到 AstrBot LLM 对话历史失败：{e}",
                exc_info=True,
            )
            return False

    async def _send_chain_with_hooks(self, session_id: str, components: list) -> bool:
        """发送消息链（含装饰钩子）。"""
        processed_chain_list = await self._trigger_decorating_hooks(
            session_id, components
        )
        if not processed_chain_list:
            return False

        # 将处理后的组件列表封装为统一消息链对象
        chain = MessageChain(processed_chain_list)
        parsed = self._parse_session_id(session_id)
        if not parsed:
            # 无法解析则使用核心 API 兜底
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return True

        p_id, m_type_str, t_id = parsed
        m_type = (
            MessageType.GROUP_MESSAGE
            if "Group" in m_type_str
            else MessageType.FRIEND_MESSAGE
        )

        # 精确匹配平台实例：避免将消息发往错误平台
        platforms = self.context.platform_manager.get_insts()
        target_platform = next((p for p in platforms if p.meta().id == p_id), None)

        if not target_platform:
            logger.warning(
                f"[主动消息] 找不到指定的平台 {p_id} 喵，尝试使用核心 API 兜底喵。"
            )
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return True

        if target_platform.status != PlatformStatus.RUNNING:
            logger.warning(f"[主动消息] 平台 {p_id} 未运行喵，跳过主动消息喵。")
            return False

        try:
            session_obj = MS(platform_name=p_id, message_type=m_type, session_id=t_id)
            await target_platform.send_by_session(session_obj, chain)
            logger.debug(f"[主动消息] 消息将通过平台 {p_id} 送达喵")
            if p_id != "webchat":
                await self._persist_proactive_message_to_platform_history(
                    session_id, chain
                )
            return True
        except Exception as e:
            logger.error(f"[主动消息] 通过平台 {p_id} 发送失败喵: {e}")
            logger.debug(traceback.format_exc())
            if self.telemetry and self.telemetry.enabled:
                # 平台发送失败是实际送达链路的问题，与 LLM 生成失败应在遥测上分开统计。
                self._track_task(
                    asyncio.create_task(
                        self.telemetry.track_error(
                            e,
                            module="core.message_sender._send_chain_with_hooks",
                        )
                    )
                )
            return False

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
        seg_conf = session_config.get("segmented_reply_settings", {})

        # 先尝试 TTS：成功后是否继续发文本由 always_send_text 控制
        is_tts_sent = False
        is_text_sent = False
        if tts_conf.get("enable_tts", True):
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        is_tts_sent = await self._send_chain_with_hooks(
                            session_id, [Record(file=audio_path)]
                        )
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")
                if self.telemetry and self.telemetry.enabled:
                    # TTS 失败不一定意味着文本发送失败，因此单独挂到 tts 子模块下记录。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_error(
                                e,
                                module="core.message_sender._send_proactive_message.tts",
                            )
                        )
                    )

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
                if self.telemetry and self.telemetry.enabled:
                    # 这里只记录分段数、文本长度、TTS 开关等统计值，不上传任何消息正文内容。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_feature(
                                "message_send_result",
                                {
                                    "session_type": session_config.get(
                                        "_session_type", "unknown"
                                    ),
                                    "tts_enabled": bool(
                                        tts_conf.get("enable_tts", True)
                                    ),
                                    "tts_sent": is_tts_sent,
                                    "segmented_enabled": True,
                                    "segment_count": len(segments),
                                    "text_length": len(text),
                                    "success": True,
                                },
                            )
                        )
                    )

                # 分段顺序发送，段间按策略等待，模拟自然输出节奏
                for idx, seg in enumerate(segments):
                    is_text_sent = (
                        await self._send_chain_with_hooks(
                            session_id,
                            [Plain(text=seg)],
                        )
                        or is_text_sent
                    )
                    if idx < len(segments) - 1:
                        interval = await self._calc_interval(seg, seg_conf)
                        logger.debug(f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。")
                        await asyncio.sleep(interval)
            else:
                is_text_sent = await self._send_chain_with_hooks(
                    session_id,
                    [Plain(text=text)],
                )
                if self.telemetry and self.telemetry.enabled:
                    # 非分段文本发送同样记录统一的发送统计，便于后续比较不同发送策略的使用占比。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_feature(
                                "message_send_result",
                                {
                                    "session_type": session_config.get(
                                        "_session_type", "unknown"
                                    ),
                                    "tts_enabled": bool(
                                        tts_conf.get("enable_tts", True)
                                    ),
                                    "tts_sent": is_tts_sent,
                                    "segmented_enabled": False,
                                    "segment_count": 1,
                                    "text_length": len(text),
                                    "success": True,
                                },
                            )
                        )
                    )

        if is_tts_sent or is_text_sent:
            cached = await self._cache_runtime_bot_message_direct(
                session_id=session_id,
                text=text,
                source="proactive_send",
            )
            if cached:
                logger.info(
                    f"[主动消息] 已记录本次主动发送内容：{self._get_session_log_str(session_id, session_config)}。"
                )

        # Bot 在群聊发言后需要重置沉默计时
        if "group" in session_id.lower():
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {self._get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )

        return bool(is_tts_sent or is_text_sent)
