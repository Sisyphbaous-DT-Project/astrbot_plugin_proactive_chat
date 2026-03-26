"""主动消息核心执行流模块。"""

from __future__ import annotations

import random
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)

from ..utils.time_utils import is_quiet_time


class ProactiveCoreMixin:
    """主动消息核心执行流混入类。"""

    data_lock: any
    session_data: dict
    last_message_times: dict[str, float]

    async def _is_chat_allowed(self, session_id: str) -> bool:
        """检查是否允许进行主动聊天（条件检查）。"""
        session_config = self._get_session_config(session_id)
        # 会话未配置或已禁用时，直接阻止本轮主动消息
        if not session_config or not session_config.get("enable", False):
            return False

        # 免打扰时段判断
        schedule_conf = session_config.get("schedule_settings", {})
        if is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), self.timezone):
            logger.info("[主动消息] 当前为免打扰时段喵。")
            return False

        return True

    async def _finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ) -> None:
        """主动消息任务完成后的收尾工作。"""
        try:
            # 存档对话历史（使用新对话管理 API）
            user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
            assistant_msg_obj = AssistantMessageSegment(
                content=[TextPart(text=assistant_response)]
            )
            await self.context.conversation_manager.add_message_pair(
                cid=conv_id,
                user_message=user_msg_obj,
                assistant_message=assistant_msg_obj,
            )
            logger.info("[主动消息] 已成功将本次主动消息存档至对话历史喵。")
        except Exception as e:
            logger.error(f"[主动消息] 存档对话历史失败喵: {e}")
            logger.warning("[主动消息] 对话存档失败喵，但会继续执行后续步骤喵。")

        async with self.data_lock:
            # 更新未回复计数器
            # 每次主动发送成功后，未回复次数 +1
            new_unanswered_count = unanswered_count + 1
            self.session_data.setdefault(session_id, {})["unanswered_count"] = (
                new_unanswered_count
            )
            logger.info(
                f"[主动消息] {self._get_session_log_str(session_id)} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
            )

            # 私聊任务：继续调度下一次
            parsed = self._parse_session_id(session_id)
            is_private_session = parsed and (
                "Friend" in parsed[1] or "Private" in parsed[1]
            )
            if is_private_session:
                session_config = self._get_session_config(session_id)
                if not session_config:
                    return

                schedule_conf = session_config.get("schedule_settings", {})
                min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
                max_interval = max(
                    min_interval,
                    int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                )
                # 私聊采用配置区间内随机间隔，减少触发规律性
                random_interval = random.randint(min_interval, max_interval)
                scheduled_at = time.time()
                next_trigger_time = scheduled_at + random_interval
                run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

                self.scheduler.add_job(
                    self.check_and_chat,
                    "date",
                    run_date=run_date,
                    args=[session_id],
                    id=session_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )

                session_payload = self.session_data.setdefault(session_id, {})
                session_payload["next_trigger_time"] = next_trigger_time
                session_payload["last_scheduled_at"] = scheduled_at
                session_payload["last_schedule_min_interval_seconds"] = min_interval
                session_payload["last_schedule_max_interval_seconds"] = max_interval
                session_payload["last_schedule_random_interval_seconds"] = (
                    random_interval
                )
                logger.info(
                    f"[主动消息] 已为 {self._get_session_log_str(session_id, session_config)} 安排下一次主动消息喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
                )

            await self._save_data_internal()

    async def check_and_chat(self, session_id: str) -> None:
        """由定时任务触发的核心函数，完成一次完整的主动消息流程。"""
        try:
            # 免打扰与启用状态检查
            if not await self._is_chat_allowed(session_id):
                logger.info("[主动消息] 当前为免打扰时段，跳过并重新调度喵。")
                await self._schedule_next_chat_and_save(session_id)
                return

            session_config = self._get_session_config(session_id)
            if not session_config:
                return

            schedule_conf = session_config.get("schedule_settings", {})

            # 未回复次数上限检查
            async with self.data_lock:
                unanswered_count = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )
                max_unanswered = schedule_conf.get("max_unanswered_times", 3)
                if max_unanswered > 0 and unanswered_count >= max_unanswered:
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息喵。"
                    )
                    return

            logger.info(
                f"[主动消息] 开始生成第 {unanswered_count + 1} 次主动消息喵，当前未回复次数: {unanswered_count} 次喵。"
            )

            # 准备上下文与人格
            request_package = await self._prepare_llm_request(session_id)
            if not request_package:
                await self._schedule_next_chat_and_save(session_id)
                return

            conv_id = request_package["conv_id"]
            history_messages = request_package["history"]
            system_prompt = request_package["system_prompt"]
            # 可能使用规范化后的会话 ID（由上下文准备阶段返回）
            session_id = request_package.get("session_id", session_id)

            # 记录任务开始状态快照
            # 用于检测 LLM 生成窗口内是否出现用户新消息
            task_start_state = {
                "last_message_time": self.last_message_times.get(session_id, 0),
                "unanswered_count": unanswered_count,
                "timestamp": time.time(),
            }

            # 调用 LLM
            response_text, final_user_prompt = await self._generate_llm_response(
                session_id,
                session_config,
                history_messages,
                system_prompt,
                unanswered_count,
            )
            if not response_text:
                await self._schedule_next_chat_and_save(session_id)
                return

            # 检查生成期间是否有新消息
            current_state = {
                "last_message_time": self.last_message_times.get(session_id, 0),
                "unanswered_count": self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                ),
            }

            # 任一条件命中都代表“用户已有新动作”，本次生成结果需丢弃
            has_new_message = (
                current_state["last_message_time"]
                > task_start_state["last_message_time"]
                or current_state["unanswered_count"]
                < task_start_state["unanswered_count"]
            )

            if has_new_message:
                logger.info(
                    "[主动消息] 检测到用户在LLM生成期间发送了新消息，丢弃本次主动消息喵。"
                )
                return

            # 发送消息与收尾
            await self._send_proactive_message(session_id, response_text)

            await self._finalize_and_reschedule(
                session_id,
                conv_id,
                final_user_prompt,
                response_text,
                unanswered_count,
            )

            # 群聊由沉默倒计时驱动，不依赖持久化 next_trigger_time，故在此清理
            parsed = self._parse_session_id(session_id)
            is_group_session = parsed and ("Group" in parsed[1] or "Guild" in parsed[1])
            if is_group_session:
                async with self.data_lock:
                    if (
                        session_id in self.session_data
                        and "next_trigger_time" in self.session_data[session_id]
                    ):
                        del self.session_data[session_id]["next_trigger_time"]
                        await self._save_data_internal()

        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)

            logger.error("[主动消息] check_and_chat 任务发生致命错误喵:")
            logger.error(f"[主动消息] 错误类型喵: {error_type}")
            logger.error(f"[主动消息] 错误信息喵: {error_msg}")

            # 清理失败任务的持久化调度痕迹，避免下次启动误恢复
            try:
                async with self.data_lock:
                    if (
                        session_id in self.session_data
                        and "next_trigger_time" in self.session_data[session_id]
                    ):
                        del self.session_data[session_id]["next_trigger_time"]
                        await self._save_data_internal()
            except Exception as clean_e:
                logger.debug(f"[主动消息] 清理失败任务数据时出错喵: {clean_e}")

            # 尝试补偿性重调度，尽量维持会话后续触发能力
            try:
                logger.info(
                    f"[主动消息] 尝试重新调度 {self._get_session_log_str(session_id)} 的主动消息任务喵。"
                )
                await self._schedule_next_chat_and_save(session_id)
                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id)} 的任务重新调度成功喵。"
                )
            except Exception as se:
                logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")
                logger.error(
                    f"[主动消息] {self._get_session_log_str(session_id)} 可能需要手动干预喵。"
                )
