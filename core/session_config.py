"""配置读取与验证模块。

包含配置校验、会话配置解析等基础逻辑。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from .group_batch_config import (
    normalize_group_batches,
    normalize_session_list,
    normalize_session_settings,
)

try:
    from ..utils.safe_logging import log_safe_exception
except ImportError:  # 允许测试直接以 core 包导入模块
    from utils.safe_logging import log_safe_exception


class ConfigMixin:
    """配置读取与验证混入类。"""

    config: dict
    session_override_manager: Any

    async def _validate_config(self) -> None:
        """验证插件配置的完整性和有效性"""
        try:
            # 读取全局配置块
            friend_settings = self.config.get("friend_settings", {})
            group_settings = self.config.get("group_settings", {})
            if not isinstance(friend_settings, dict):
                friend_settings = {}
            if not isinstance(group_settings, dict):
                group_settings = {}

            # 私聊配置校验
            if friend_settings.get("enable", False):
                session_list = normalize_session_list(
                    friend_settings.get("session_list", []),
                    path="friend_settings.session_list",
                )
                if not session_list:
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

                # 调度区间合法性由统一归一化器兜底，坏值不会阻断启动。
                normalized_friend = normalize_session_settings(
                    friend_settings,
                    session_type="friend",
                    fill_defaults=True,
                    path="friend_settings",
                )
                schedule_settings = normalized_friend.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)
                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊主动消息配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

            # 群聊配置校验
            if group_settings.get("enable", False):
                session_list = normalize_session_list(
                    group_settings.get("session_list", []),
                    path="group_settings.session_list",
                )
                batches = normalize_group_batches(
                    self.config.get("group_batches", []),
                    fill_defaults=False,
                )
                has_batches = bool(batches)
                if not session_list and not has_batches:
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（session_list 为空且无批次）。"
                    )

                # 校验群聊批次的区间合法性
                for batch in batches:
                    min_interval = batch.get("min_interval_minutes", 90)
                    max_interval = batch.get("max_interval_minutes", 360)
                    if min_interval > max_interval:
                        logger.warning(
                            "[主动消息] 某个群聊批次的最小间隔大于最大间隔喵，将自动调整喵。"
                        )

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            log_safe_exception(
                logger,
                "error",
                "PC-CONFIG-001",
                "配置验证过程出错",
                e,
            )
            raise

    def _get_session_config(self, session_id: str) -> dict | None:
        """根据会话 UMO 获取最终生效配置（base + override）。"""
        base = self._get_base_session_config(session_id)
        if not base:
            return None
        return self._build_effective_config(session_id, base)

    def _get_base_session_config(self, session_id: str) -> dict | None:
        """获取仅由全局配置与会话命中规则决定的基础配置。"""
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return None

        _, message_type, target_id = parsed
        # 根据消息类型路由到不同配置区块（私聊/群聊）。
        # AstrBot 不同适配器可能使用 FriendMessage/PrivateMessage，群聊
        # 可能使用 GroupMessage/GuildMessage；不能只匹配其中一个名称。
        if message_type in {"FriendMessage", "PrivateMessage"}:
            return self._get_typed_session_config(
                session_id, target_id, "friend_settings", "friend"
            )
        if message_type in {"GroupMessage", "GuildMessage"}:
            return self._get_typed_session_config(
                session_id, target_id, "group_settings", "group"
            )
        return None

    def _build_effective_config(
        self, session_id: str, base_config: dict | None
    ) -> dict | None:
        """将会话差异补丁合并到基础配置，返回最终生效配置。"""
        if not base_config:
            return None

        manager = getattr(self, "session_override_manager", None)
        if not manager:
            return base_config

        normalized_session_id = self._normalize_session_id(session_id)
        effective = manager.get_effective(normalized_session_id, base_config)

        if isinstance(effective, dict):
            effective = normalize_session_settings(
                effective,
                session_type=base_config.get("_session_type", "friend"),
                fill_defaults=True,
                path="session_effective",
            )
            # 保留运行时元信息，避免被白名单过滤丢失
            effective["_session_type"] = base_config.get("_session_type")
            effective["_from_session_list"] = base_config.get(
                "_from_session_list", False
            )
            effective["_from_batch"] = base_config.get("_from_batch")
            effective["_has_override"] = bool(
                manager.get_override(normalized_session_id)
            )

        return effective

    def _get_typed_session_config(
        self, session_id: str, target_id: str, settings_key: str, session_type: str
    ) -> dict | None:
        # 配置仅在 enable 且命中 session_list 时生效
        raw_settings = self.config.get(settings_key, {})
        if not isinstance(raw_settings, dict):
            return None
        settings = normalize_session_settings(
            raw_settings,
            session_type=session_type,
            fill_defaults=True,
            path=settings_key,
        )
        if not settings.get("enable", False):
            return None

        normalized_session_id = self._normalize_session_id(session_id)
        candidates = {session_id, normalized_session_id, target_id}

        # === 群聊批次优先检查 ===
        if session_type == "group" and settings_key == "group_settings":
            batches = normalize_group_batches(
                self.config.get("group_batches", []),
                fill_defaults=False,
            )
            for batch in batches:
                batch_session_list = batch.get("session_list", [])
                if any(candidate in batch_session_list for candidate in candidates):
                    # 以全局群聊配置为基座，用批次字段覆盖
                    config_copy = copy.deepcopy(settings)
                    config_copy["_session_type"] = session_type
                    config_copy["_from_session_list"] = True
                    config_copy["_from_batch"] = batch.get("batch_name", "未命名批次")

                    # 覆盖批次扁平字段
                    for key in ("group_idle_trigger_minutes", "proactive_prompt"):
                        if key in batch and batch[key]:
                            config_copy[key] = batch[key]

                    # 组装 schedule_settings（批次扁平字段 → 嵌套结构）
                    raw_schedule = config_copy.get("schedule_settings", {})
                    schedule_copy = copy.deepcopy(
                        raw_schedule if isinstance(raw_schedule, dict) else {}
                    )
                    for key in (
                        "min_interval_minutes",
                        "max_interval_minutes",
                        "quiet_hours",
                        "max_unanswered_times",
                    ):
                        if key in batch:
                            schedule_copy[key] = batch[key]
                    # 批次只覆盖一端时，也要对“继承后的最终区间”做一次兜底，
                    # 避免旧配置出现 min > max 后把调度器带入异常状态。
                    try:
                        if schedule_copy.get(
                            "min_interval_minutes", 0
                        ) > schedule_copy.get("max_interval_minutes", 0):
                            schedule_copy["max_interval_minutes"] = schedule_copy[
                                "min_interval_minutes"
                            ]
                    except (TypeError, ValueError):
                        schedule_copy["min_interval_minutes"] = 90
                        schedule_copy["max_interval_minutes"] = 360
                    config_copy["schedule_settings"] = schedule_copy

                    return normalize_session_settings(
                        config_copy,
                        session_type=session_type,
                        fill_defaults=True,
                        path="group_settings",
                    )

        # 命中全局 session_list
        session_list = normalize_session_list(settings.get("session_list", []))
        if any(candidate in session_list for candidate in candidates):
            # 返回深拷贝，避免调用方意外修改全局配置对象
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = session_type
            config_copy["_from_session_list"] = True
            return normalize_session_settings(
                config_copy,
                session_type=session_type,
                fill_defaults=True,
                path=settings_key,
            )

        return None

    def _get_friend_session_config(
        self, session_id: str, target_id: str
    ) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "friend_settings", "friend"
        )

    def _get_group_session_config(self, session_id: str, target_id: str) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "group_settings", "group"
        )
