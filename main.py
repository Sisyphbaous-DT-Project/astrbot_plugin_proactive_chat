# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: v1.2.0

"""插件入口与主类定义。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

# 导入各模块的 Mixins，用于组装插件能力
from .core.chat_flow import ProactiveCoreMixin
from .core.data_storage import StorageMixin
from .core.group_batch_config import normalize_group_batches
from .core.llm_adapter import LlmMixin
from .core.message_events import EventsMixin
from .core.message_sender import SenderMixin
from .core.notification_center import NotificationCenter
from .core.plugin_lifecycle import LifecycleMixin
from .core.runtime_context_cache import RuntimeContextCache, RuntimeContextCacheMixin
from .core.session_config import ConfigMixin
from .core.session_override_manager import SessionOverrideManager
from .core.session_parser import SessionMixin
from .core.task_scheduler import SchedulerMixin
from .core.web_admin_server import WebAdminServer
from .utils.safe_logging import log_safe_exception


class ProactiveChatPlugin(
    SessionMixin,  # 会话 ID 解析、规范化与日志格式化
    StorageMixin,  # 会话数据加载/保存与迁移清理
    ConfigMixin,  # 配置读取与会话级配置路由
    SchedulerMixin,  # 定时任务、自动触发与沉默计时
    RuntimeContextCacheMixin,  # 插件运行时最近聊天记录
    LlmMixin,  # 上下文准备与 LLM 调用封装
    SenderMixin,  # 主动消息发送与装饰钩子
    EventsMixin,  # 私聊/群聊事件监听处理
    LifecycleMixin,  # initialize/terminate 生命周期管理
    ProactiveCoreMixin,  # 主动消息主流程编排
    star.Star,
):
    """
    插件的主类，负责生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        # 注入的配置对象（由 AstrBot 框架提供）
        self.config: AstrBotConfig = config
        # 静态 Schema 必须兼容 AstrBot 4.8；新版面板支持模板列表时，
        # 再在内存中升级批次编辑器，避免旧版加载阶段直接拒绝插件。
        self._normalize_group_batches_for_runtime()
        self._prepare_group_batches_schema()
        # 调度器与时区会在 initialize 中初始化
        self.scheduler = None  # AsyncIOScheduler 实例（initialize 中创建）
        self.timezone = None  # ZoneInfo 时区对象（initialize 中加载）

        # 使用 StarTools 获取插件专属数据目录（Path 对象）
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"
        self.runtime_cache_file = self.data_dir / "runtime_context_cache.json"

        # 共享锁与持久化数据容器
        self.data_lock = None
        self.session_data: dict = {}
        self.runtime_context_cache = RuntimeContextCache()
        self.runtime_cache_dirty_sessions: set[str] = set()
        self.runtime_cache_save_task: asyncio.Task[None] | None = None
        self.runtime_cache_save_delay_seconds = 2.0
        # 记录当前正在执行“立即触发”的会话，防止重复点击导致并发主动消息。
        self.manual_trigger_sessions: set[str] = set()

        # 会话差异配置管理器、通知中心与 Web 管理端
        self.session_override_manager = SessionOverrideManager(self.data_dir)
        self.notification_center = NotificationCenter(self)
        web_admin_config = self.config.get("web_admin", {})
        if isinstance(web_admin_config, dict) and web_admin_config.get(
            "enabled", False
        ):
            try:
                self.web_admin_server = WebAdminServer(self)
            except Exception as e:
                # Web 管理端属于增强能力，创建失败时仅禁用控制台，不影响插件主体继续加载。
                self.web_admin_server = None
                log_safe_exception(
                    logger,
                    "error",
                    "PC-WEB-001",
                    "Web 管理端组件创建失败，已自动禁用",
                    e,
                )
        else:
            self.web_admin_server = None
            logger.info("[主动消息] Web 管理端未启用喵，跳过独立 Web 服务初始化。")
        # 保存所有由插件创建的后台任务，终止时统一取消并等待收尾。
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # 生命周期终止期间拒绝新任务，避免计时器或管理端请求在清理窗口重新创建工作。
        self._terminating = False

        # 群聊沉默倒计时与自动触发计时器
        self.group_timers: dict[str, asyncio.TimerHandle] = {}
        self.last_bot_message_time = 0  # 预留字段：记录 Bot 最近发言时间
        self.session_temp_state: dict[
            str, dict
        ] = {}  # 临时态（如群聊最后用户发言时间）
        self.last_message_times: dict[str, float] = {}  # 会话最近消息时间，用于触发判断
        self.auto_trigger_timers: dict[
            str, asyncio.TimerHandle
        ] = {}  # 自动触发计时器句柄
        # 插件启动时间与日志控制
        self.plugin_start_time = time.time()
        self.first_message_logged: set[str] = set()
        self._cleanup_counter = 0

        logger.info("[主动消息] 插件实例已创建喵。")

    @staticmethod
    def _astrbot_supports_template_list() -> bool:
        """检测当前 AstrBot 是否认识 template_list 配置类型。"""
        try:
            from astrbot.core.config.astrbot_config import DEFAULT_VALUE_MAP
        except (ImportError, AttributeError):
            return False
        return (
            isinstance(DEFAULT_VALUE_MAP, dict) and "template_list" in DEFAULT_VALUE_MAP
        )

    def _prepare_group_batches_schema(self) -> None:
        """在新版 AstrBot 中恢复群聊批次的对象模板编辑器。"""
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict) or not self._astrbot_supports_template_list():
            return

        group_schema = schema.get("group_batches")
        if not isinstance(group_schema, dict):
            return
        if group_schema.get("type") == "template_list":
            self._normalize_group_batches_for_runtime()
            return
        if group_schema.get("type") != "list":
            return

        item_schema = group_schema.get("items")
        if not isinstance(item_schema, dict):
            return
        if item_schema.get("type") == "object" and isinstance(
            item_schema.get("items"), dict
        ):
            item_schema = item_schema["items"]

        group_schema["type"] = "template_list"
        group_schema.pop("items", None)
        group_schema["templates"] = {
            "group_batch": {
                "name": "群聊批次",
                "hint": "一组共享相同主动消息策略的群聊",
                "items": item_schema,
            }
        }

        batches = self.config.get("group_batches", [])
        if isinstance(batches, list):
            for batch in batches:
                if isinstance(batch, dict):
                    batch.setdefault("__template_key", "group_batch")

    def _normalize_group_batches_for_runtime(self) -> None:
        """归一化批次数据，并在新版 AstrBot 中补内部模板标识。"""
        self.config["group_batches"] = normalize_group_batches(
            self.config.get("group_batches", []),
            add_template_key=self._astrbot_supports_template_list(),
            fill_defaults=False,
        )

    def _track_task(self, task: asyncio.Task[Any] | None) -> asyncio.Task[Any] | None:
        """登记插件后台任务，并在异常完成时安全消费异常。"""
        if task is None:
            return None
        if getattr(self, "_terminating", False):
            task.add_done_callback(self._on_background_task_done)
            task.cancel()
            return task
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        """消费插件后台任务异常，避免 asyncio 默认日志泄露异常正文。"""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except BaseException as exception:
            log_safe_exception(
                logger,
                "error",
                "PC-ASYNC-004",
                "读取后台任务结果失败",
                exception,
            )
            return
        if error is not None:
            log_safe_exception(
                logger,
                "error",
                "PC-ASYNC-005",
                "插件后台任务异常结束",
                error,
            )

    async def _cleanup_background_tasks(self) -> None:
        """清理所有仍在运行的插件后台任务。"""
        if not self._background_tasks:
            return

        pending_tasks = list(self._background_tasks)
        for task in pending_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._background_tasks.difference_update(pending_tasks)

    async def terminate(self) -> None:
        """插件终止入口：委托 LifecycleMixin 清理。"""
        await LifecycleMixin.terminate(self)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_friend_message(self, event: AstrMessageEvent) -> None:
        """私聊消息入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_friend_message(self, event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊消息入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_group_message(self, event)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_after_message_sent(self, event)
