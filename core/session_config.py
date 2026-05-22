"""配置读取与验证模块。

包含配置校验、会话配置解析等基础逻辑。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger


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

            # 私聊配置校验
            if friend_settings.get("enable", False):
                session_list = friend_settings.get("session_list", [])
                if not session_list:
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

                # 调度区间合法性
                schedule_settings = friend_settings.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)
                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊主动消息配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

            # 群聊配置校验
            if group_settings.get("enable", False):
                session_list = group_settings.get("session_list", [])
                has_batches = bool(self.config.get("group_batches", []))
                if not session_list and not has_batches:
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（session_list 为空且无批次）。"
                    )

                # 校验群聊批次的区间合法性
                for batch in self.config.get("group_batches", []):
                    min_interval = batch.get("min_interval_minutes", 90)
                    max_interval = batch.get("max_interval_minutes", 360)
                    if min_interval > max_interval:
                        logger.warning(
                            f"[主动消息] 群聊批次 '{batch.get('batch_name', '未命名')}' 中最小间隔大于最大间隔喵，将自动调整喵。"
                        )

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
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
        # 根据消息类型路由到不同配置区块（私聊/群聊）
        # FriendMessage / PrivateMessage 均归为私聊配置
        if "Friend" in message_type:
            return self._get_typed_session_config(
                session_id, target_id, "friend_settings", "friend"
            )
        # GroupMessage / GuildMessage 均归为群聊配置
        if "Group" in message_type:
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
        settings = self.config.get(settings_key, {})
        if not settings.get("enable", False):
            return None

        normalized_session_id = self._normalize_session_id(session_id)
        candidates = {session_id, normalized_session_id, target_id}

        # === 群聊批次优先检查 ===
        if session_type == "group" and settings_key == "group_settings":
            batches = self.config.get("group_batches", [])
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
                    schedule_copy = copy.deepcopy(
                        config_copy.get("schedule_settings", {})
                    )
                    for key in (
                        "min_interval_minutes",
                        "max_interval_minutes",
                        "quiet_hours",
                        "max_unanswered_times",
                    ):
                        if key in batch:
                            schedule_copy[key] = batch[key]
                    config_copy["schedule_settings"] = schedule_copy

                    return config_copy

        # 命中全局 session_list
        session_list = settings.get("session_list", [])
        if any(candidate in session_list for candidate in candidates):
            # 返回深拷贝，避免调用方意外修改全局配置对象
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = session_type
            config_copy["_from_session_list"] = True
            return config_copy

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
