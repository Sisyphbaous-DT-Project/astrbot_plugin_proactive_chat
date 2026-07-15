"""插件生命周期模块。"""

from __future__ import annotations

import asyncio
import zoneinfo
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger

try:
    from ..utils.safe_logging import log_safe_exception
except ImportError:  # 允许测试直接以 core 包导入模块
    from utils.safe_logging import log_safe_exception


class LifecycleMixin:
    """插件生命周期混入类。"""

    context: star.Context
    data_lock: asyncio.Lock
    plugin_start_time: float
    manual_trigger_sessions: set[str]
    scheduler: AsyncIOScheduler
    timezone: zoneinfo.ZoneInfo | None
    session_data: dict
    last_message_times: dict[str, float]
    group_timers: dict[str, asyncio.TimerHandle]
    auto_trigger_timers: dict[str, asyncio.TimerHandle]
    data_dir: Any
    session_data_file: Any
    web_admin_server: Any
    notification_center: Any

    async def initialize(self) -> None:
        """插件的异步初始化函数。"""
        # 初始化共享锁
        self.data_lock = asyncio.Lock()

        # 配置校验（异常不阻断启动）
        try:
            await self._validate_config()
        except Exception as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-LIFECYCLE-000",
                "配置验证发现问题，将继续使用默认设置",
                e,
            )

        # 加载持久化数据
        async with self.data_lock:
            await self._load_data_internal()
            # 启动时先做会话键规范化，避免历史数据中的多键并存
            normalized = self._normalize_session_data()
            if normalized:
                # 仅在发生规范化变更时回写，减少无效 IO
                await self._save_data_internal()
        logger.info("[主动消息] 已成功从文件加载会话数据喵。")
        await self._load_runtime_context_cache_from_disk()

        # 恢复插件启动后的消息时间（用于自动触发判定）
        restored_count = 0
        for session_id, session_info in self.session_data.items():
            if isinstance(session_info, dict) and "last_message_time" in session_info:
                last_time = session_info["last_message_time"]
                if isinstance(last_time, (int, float)) and last_time > 0:
                    # 仅恢复“本次启动后”的消息时间，避免历史消息误触发逻辑
                    if last_time >= self.plugin_start_time:
                        self.last_message_times[session_id] = last_time
                        restored_count += 1
                        logger.debug(
                            f"[主动消息] 已恢复 {self._get_session_log_str(session_id)} 在插件启动后的消息时间喵 -> {last_time}"
                        )
                    else:
                        logger.debug(
                            f"[主动消息] 忽略插件启动前的历史消息时间用于自动主动消息任务喵: {self._get_session_log_str(session_id)} -> {last_time}"
                        )

        if restored_count > 0:
            logger.info(
                f"[主动消息] 已从持久化数据恢复 {restored_count} 个会话在插件启动后的消息时间喵。"
            )

        # 读取时区设置（失败时回退系统时区）
        try:
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            log_safe_exception(
                logger,
                "warning",
                "PC-LIFECYCLE-015",
                "时区配置无效或未配置，将使用服务器系统时区",
                e,
            )
            self.timezone = None

        # 启动调度器
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        # 先恢复持久化任务，再初始化自动触发器，避免重复调度
        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化喵。")

        await self._setup_auto_triggers_for_enabled_sessions()
        logger.info("[主动消息] 自动主动消息触发器初始化完成喵。")

        # 启动通知系统
        try:
            if self.notification_center:
                await self.notification_center.start()
        except Exception as e:
            log_safe_exception(
                logger,
                "error",
                "PC-LIFECYCLE-001",
                "通知系统启动失败",
                e,
            )

        # 启动 Web 管理端
        try:
            if self.web_admin_server:
                await self.web_admin_server.start()
        except (SystemExit, OSError) as e:
            log_safe_exception(
                logger,
                "error",
                "PC-LIFECYCLE-002",
                "Web 管理端启动失败，已隔离处理",
                e,
            )
        except Exception as e:
            log_safe_exception(
                logger,
                "error",
                "PC-LIFECYCLE-003",
                "Web 管理端启动失败",
                e,
            )

    async def terminate(self) -> None:
        """插件被卸载或停用时调用的清理函数。"""
        logger.info("[主动消息] 收到插件终止指令，开始清理资源喵。")
        self._terminating = True
        try:
            # 取消群聊沉默计时器
            timer_count = len(self.group_timers)
            for session_id, timer in self.group_timers.items():
                try:
                    timer.cancel()
                    logger.debug(
                        f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的沉默计时器喵。"
                    )
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "warning",
                        "PC-LIFECYCLE-005",
                        "取消计时器时出错",
                        e,
                    )

            self.group_timers.clear()
            logger.info(
                f"[主动消息] 已取消 {timer_count} 个正在运行的群聊沉默计时器喵。"
            )

            # 取消自动触发计时器
            auto_trigger_count = len(self.auto_trigger_timers)
            for session_id, timer in list(self.auto_trigger_timers.items()):
                try:
                    timer.cancel()
                    logger.debug(
                        f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的自动触发计时器喵。"
                    )
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "warning",
                        "PC-LIFECYCLE-006",
                        "取消自动触发计时器时出错",
                        e,
                    )

            self.auto_trigger_timers.clear()
            logger.info(f"[主动消息] 已取消 {auto_trigger_count} 个自动触发计时器喵。")

            # 计时器和入口已封住后，再停止会创建异步工作的附加组件，避免它们
            # 在任务清理窗口重新投递主动消息或通知轮询。
            if self.web_admin_server:
                try:
                    await self.web_admin_server.stop()
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "warning",
                        "PC-LIFECYCLE-011",
                        "停止 Web 管理端时出错",
                        e,
                    )

            if self.notification_center:
                try:
                    await self.notification_center.stop()
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "warning",
                        "PC-LIFECYCLE-012",
                        "停止通知系统时出错",
                        e,
                    )

            await self._cleanup_background_tasks()

            # 终止前写入最近聊天记录，避免插件重载后丢失已记录的上下文
            try:
                await self._flush_runtime_context_cache_save()
            except Exception as e:
                log_safe_exception(
                    logger,
                    "error",
                    "PC-LIFECYCLE-007",
                    "保存最近聊天记录时出错",
                    e,
                )

            # 清理调度器任务（逐个移除后再 shutdown，便于日志定位）
            if self.scheduler and self.scheduler.running:
                try:
                    jobs = self.scheduler.get_jobs()
                    logger.info(f"[主动消息] 正在清理调度器任务喵，数量: {len(jobs)}")
                    for job in jobs:
                        try:
                            self.scheduler.remove_job(job.id)
                            logger.debug("[主动消息] 已移除一条调度器任务喵。")
                        except Exception as e:
                            log_safe_exception(
                                logger,
                                "warning",
                                "PC-LIFECYCLE-008",
                                "移除调度器任务时出错",
                                e,
                            )

                    self.scheduler.shutdown()
                    logger.info("[主动消息] 调度器已关闭喵。")
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "error",
                        "PC-LIFECYCLE-009",
                        "关闭调度器时出错",
                        e,
                    )

            # 终止前最后一次持久化，尽量保留当前会话状态
            if self.data_lock:
                try:
                    async with self.data_lock:
                        await self._save_data_internal()
                    logger.info("[主动消息] 会话数据已保存喵。")
                except Exception as e:
                    log_safe_exception(
                        logger,
                        "error",
                        "PC-LIFECYCLE-010",
                        "保存数据时出错",
                        e,
                    )

        except Exception as e:
            log_safe_exception(
                logger,
                "error",
                "PC-LIFECYCLE-013",
                "生命周期终止阶段发生异常",
                e,
            )
        finally:
            # 确保终止日志一定输出
            logger.info("[主动消息] 主动消息插件已终止喵。")
