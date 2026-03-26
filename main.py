# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: v1.2.0

"""插件入口与主类定义。"""

from __future__ import annotations

import asyncio
import time

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

# 导入各模块的 Mixins，用于组装插件能力
from .core.chat_flow import ProactiveCoreMixin
from .core.data_storage import StorageMixin
from .core.llm_adapter import LlmMixin
from .core.message_events import EventsMixin
from .core.message_sender import SenderMixin
from .core.plugin_lifecycle import LifecycleMixin
from .core.session_config import ConfigMixin
from .core.session_override_manager import SessionOverrideManager
from .core.session_parser import SessionMixin
from .core.task_scheduler import SchedulerMixin
from .core.web_admin_server import WebAdminServer


class ProactiveChatPlugin(
    SessionMixin,  # 会话 ID 解析、规范化与日志格式化
    StorageMixin,  # 会话数据加载/保存与迁移清理
    ConfigMixin,  # 配置读取与会话级配置路由
    SchedulerMixin,  # 定时任务、自动触发与沉默计时
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
        # 调度器与时区会在 initialize 中初始化
        self.scheduler = None  # AsyncIOScheduler 实例（initialize 中创建）
        self.timezone = None  # ZoneInfo 时区对象（initialize 中加载）

        # 使用 StarTools 获取插件专属数据目录（Path 对象）
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"

        # 共享锁与持久化数据容器
        self.data_lock = None
        self.session_data: dict = {}

        # 会话差异配置管理器与 Web 管理端
        self.session_override_manager = SessionOverrideManager(self.data_dir)
        self.web_admin_server = WebAdminServer(self)

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
