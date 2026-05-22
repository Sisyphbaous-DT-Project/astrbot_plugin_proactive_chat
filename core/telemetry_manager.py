"""主动消息插件遥测管理模块。"""

from __future__ import annotations

import asyncio
import base64
import copy
import platform
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.star import StarTools

from ..utils.version import get_astrbot_version_info


class TelemetryManager:
    """遥测管理器，负责匿名上报插件运行状态、配置快照与错误信息。"""

    # 统一的遥测接收端地址
    _ENDPOINT = "https://telemetry.aloys233.top/api/ingest"
    # 当前项目专用的 App Key 以 Base64 形式存放，运行时再解码使用。
    _ENCODED_APP_KEY = "dGtfODlpS0tBd2VhX3RFVVZSbll2cl9JR3Jld0tsaXhVdzI="
    _APP_KEY = base64.b64decode(_ENCODED_APP_KEY).decode()
    # 插件名用于定位插件私有数据目录，并持久化实例级匿名 ID。
    _PLUGIN_NAME = "astrbot_plugin_proactive_chat"
    # 匹配统一消息来源 UMO，避免错误日志或异常文本把真实会话标识直接上传到遥测平台。
    _UMO_PATTERN = re.compile(
        r"(?P<umo>[A-Za-z0-9_.-]+:(?:FriendMessage|PrivateMessage|GroupMessage|GuildMessage):[^\s,'\"]+)"
    )
    # 匹配 password/token/key/secret 这类典型敏感键值，做统一掩码替换。
    _KEY_VALUE_PATTERN = re.compile(
        r"(?i)(password|token|secret|api[_-]?key|app[_-]?key)(\s*[=:]\s*)([^,\s]+)"
    )
    # 匹配 prompt / system_prompt / proactive_prompt 字段，确保提示词正文被整段过滤。
    _PROMPT_FIELD_PATTERN = re.compile(
        r'(?is)("?(?:proactive_prompt|system_prompt|prompt)"?\s*[:=]\s*)(.+?)(?=,\s*"?[A-Za-z0-9_]+"?\s*[:=]|\}|\]|$)'
    )

    def __init__(self, config: dict[str, Any], plugin_version: str = "unknown") -> None:
        # 保留原始配置引用，方便后续在需要时提取配置快照或扩展更多上下文字段。
        self._config = config
        # 版本号由统一版本工具提供，避免不同模块各自解析 metadata 造成不一致。
        self._plugin_version = plugin_version
        # 遥测默认开启，只有用户明确在 telemetry_config 中关闭时才停用。
        telemetry_config = config.get("telemetry_config", {})
        self._enabled = telemetry_config.get("enabled", True)
        # 为当前安装实例生成稳定匿名 ID，用于跨重启聚合同一实例的遥测事件。
        self._instance_id = self._get_or_create_instance_id()
        # HTTP session 延迟初始化，只有真正发请求时才创建，减少空载资源占用。
        self._session: aiohttp.ClientSession | None = None
        # 当前先固定为 production，后续若接入测试环境可在这里扩展切换。
        self._env = "production"
        # AstrBot 版本在启动时一并上报，便于区分宿主版本差异带来的兼容性问题。
        self._astrbot_version_info = get_astrbot_version_info()
        self._astrbot_version = self._astrbot_version_info.version

        if self._enabled:
            logger.debug(
                f"[主动消息] 已启用匿名遥测喵 (Instance ID: {self._instance_id}, Version: {self._plugin_version})"
            )
        else:
            logger.debug("[主动消息] 遥测功能未启用喵。")

    @property
    def enabled(self) -> bool:
        """是否启用遥测。"""
        return self._enabled

    def _get_or_create_instance_id(self) -> str:
        """获取或创建实例 ID。"""
        try:
            # 使用 AstrBot 提供的插件数据目录，确保不同插件不会互相覆盖实例 ID 文件。
            data_dir = StarTools.get_data_dir(self._PLUGIN_NAME)
            id_file = data_dir / ".telemetry_id"
            if id_file.exists():
                # 若实例 ID 已存在则直接复用，保证同一安装实例的遥测身份长期稳定。
                instance_id = id_file.read_text(encoding="utf-8").strip()
                if instance_id:
                    return instance_id

            # 首次运行时生成新的 UUID，并持久化到插件数据目录。
            instance_id = str(uuid.uuid4())
            data_dir.mkdir(parents=True, exist_ok=True)
            id_file.write_text(instance_id, encoding="utf-8")
            logger.debug(f"[主动消息] 已生成新的遥测实例 ID 喵: {instance_id}")
            return instance_id
        except Exception as e:
            logger.warning(f"[主动消息] 无法持久化遥测实例 ID 喵: {e}")
            return str(uuid.uuid4())

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话。"""
        if self._session is None or self._session.closed:
            # 遥测必须是 best-effort，因此总超时设置得比较短，避免卡住主业务。
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def track(self, event_name: str, data: dict[str, Any] | None = None) -> bool:
        """发送遥测事件。"""
        if not self._enabled:
            return False

        # 服务端 ingest API 采用 batch 结构，这里即使单次只发一条，也统一按 batch 协议封装。
        payload = {
            "instance_id": self._instance_id,
            "version": self._plugin_version,
            "env": self._env,
            "batch": [
                {
                    "event": event_name,
                    # 事件数据必须由上层先完成脱敏与裁剪；发送层只负责透传。
                    "data": data or {},
                    # 统一使用 UTC ISO 时间，便于服务端直接聚合与排序。
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

        try:
            session = await self._get_session()
            # App Key 通过请求头发送，避免混入事件 data 字段而被误统计或误暴露。
            headers = {
                "Content-Type": "application/json",
                "X-App-Key": self._APP_KEY,
            }
            async with session.post(
                self._ENDPOINT,
                json=payload,
                headers=headers,
            ) as response:
                if response.status == 200:
                    logger.debug(f"[主动消息] 遥测事件 '{event_name}' 发送成功喵。")
                    return True
                if response.status == 401:
                    logger.warning("[主动消息] 遥测 App Key 无效或项目已禁用喵。")
                    return False
                if response.status == 429:
                    logger.warning("[主动消息] 遥测请求频率超限喵。")
                    return False
                logger.debug(f"[主动消息] 遥测事件发送失败喵: HTTP {response.status}")
                return False
        except asyncio.TimeoutError:
            logger.debug("[主动消息] 遥测请求超时喵。")
            return False
        except aiohttp.ClientConnectionError as e:
            logger.debug(f"[主动消息] 遥测连接失败喵: {e}")
            return False
        except aiohttp.ClientPayloadError as e:
            logger.debug(f"[主动消息] 遥测数据负载错误喵: {e}")
            return False
        except aiohttp.ClientError as e:
            logger.debug(f"[主动消息] 遥测网络错误喵: {e}")
            return False
        except Exception as e:
            logger.debug(f"[主动消息] 遥测未知错误喵: {e}")
            return False

    async def track_startup(self) -> bool:
        """上报启动事件。"""
        return await self.track(
            "startup",
            {
                "os": platform.system(),
                "os_version": platform.release(),
                "python_version": platform.python_version(),
                "arch": platform.machine(),
                "astrbot_version": self._astrbot_version,
                "astrbot_version_source": self._astrbot_version_info.source,
                "astrbot_version_error": self._astrbot_version_info.error,
            },
        )

    async def track_shutdown(
        self, exit_code: int = 0, runtime_seconds: float = 0
    ) -> bool:
        """上报停止事件。"""
        return await self.track(
            "shutdown",
            {
                "exit_code": exit_code,
                "runtime_seconds": runtime_seconds,
            },
        )

    async def track_heartbeat(self, uptime_seconds: float = 0) -> bool:
        """上报心跳事件。"""
        return await self.track(
            "heartbeat",
            {
                "uptime_seconds": uptime_seconds,
            },
        )

    async def track_feature(
        self, feature_name: str, extra: dict[str, Any] | None = None
    ) -> bool:
        """上报功能使用事件。"""
        data = dict(extra or {})
        data["feature"] = feature_name
        return await self.track("feature", data)

    async def track_config(self, config: dict[str, Any]) -> bool:
        """上报配置快照，过滤敏感字段但保留统计型配置。"""
        if not self._enabled:
            return False

        try:
            # 深拷贝后再修改，避免遥测过滤逻辑意外污染插件运行中的真实配置对象。
            config_copy = copy.deepcopy(config)

            friend_settings = config_copy.get("friend_settings")
            if isinstance(friend_settings, dict):
                # 私聊会话列表不上传原文，只保留数量用于统计默认值设计是否合理。
                session_list = friend_settings.pop("session_list", [])
                friend_settings["session_list_count"] = (
                    len(session_list) if isinstance(session_list, list) else 0
                )
                # 私聊主动提示词正文属于高敏感内容，必须完全移除。
                if "proactive_prompt" in friend_settings:
                    del friend_settings["proactive_prompt"]

            group_settings = config_copy.get("group_settings")
            if isinstance(group_settings, dict):
                # 群聊会话列表同样只统计数量，不上传任何具体 UMO。
                session_list = group_settings.pop("session_list", [])
                group_settings["session_list_count"] = (
                    len(session_list) if isinstance(session_list, list) else 0
                )
                # 群聊提示词也做整段过滤，避免活跃策略被直接暴露。
                if "proactive_prompt" in group_settings:
                    del group_settings["proactive_prompt"]

            web_admin = config_copy.get("web_admin")
            if isinstance(web_admin, dict) and "password" in web_admin:
                # 管理端密码绝不进入遥测事件。
                del web_admin["password"]

            return await self.track("config", config_copy)
        except Exception as e:
            logger.debug(f"[主动消息] 配置快照提取失败喵: {e}")
            return False

    async def track_error(
        self, exception: Exception, module: str | None = None
    ) -> bool:
        """上报错误事件。"""
        # 错误消息与堆栈都必须先脱敏，再做截断，避免截断破坏正则匹配从而导致敏感内容漏网。
        raw_message = str(exception)
        sanitized_message = self._sanitize_message(raw_message)
        stack = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        sanitized_stack = self._sanitize_stack(stack)

        data = {
            "type": type(exception).__name__,
            "message": sanitized_message[:500],
            "module": module,
            "severity": "error",
            "stack": sanitized_stack[:4000],
        }
        return await self.track("error", data)

    def _sanitize_stack(self, stack: str) -> str:
        """脱敏堆栈与潜在敏感文本。"""
        # 先替换用户目录路径，避免把系统用户名或宿主机目录结构暴露出去。
        stack = re.sub(r"[A-Za-z]:\\Users\\[^\\]+\\", r"<USER_HOME>\\", stack)
        stack = re.sub(r"/(?:home|Users|root)/[^/]+/", r"<USER_HOME>/", stack)
        # 插件内部路径统一折叠成相对占位，保留定位意义但隐藏真实安装位置。
        stack = re.sub(r".*astrbot_plugin_proactive_chat[/\\]", r"<PLUGIN>/", stack)
        # site-packages 同样做路径折叠，避免把环境细节大量上传到遥测平台。
        stack = re.sub(r".*site-packages[/\\]", r"<SITE_PACKAGES>/", stack)
        # 对其他 Windows / Unix 绝对路径做保底折叠，减少非用户目录路径泄露。
        stack = re.sub(r"\b[A-Za-z]:\\[^\r\n'\"<>|]*", "<WINDOWS_PATH>", stack)
        stack = re.sub(r"(?<!<)/(?:[^\s/'\"]+/)+[^\s/'\"]+", "<UNIX_PATH>", stack)
        # 最后再复用消息脱敏规则，继续过滤 UMO、Prompt 和 key/value 等业务敏感信息。
        stack = self._sanitize_message(stack)
        return stack

    def _sanitize_message(self, message: str) -> str:
        """脱敏错误消息中的路径、UMO、Prompt 与密钥类字段。"""
        sanitized = message
        # 先过滤系统路径，防止异常字符串中直接包含宿主机用户名或完整目录。
        sanitized = re.sub(r"/(?:home|Users|root)/[^/\s]+/", r"<USER_HOME>/", sanitized)
        sanitized = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+\\", r"<USER_HOME>\\", sanitized)
        sanitized = re.sub(r"\b[A-Za-z]:\\[^\r\n'\"<>|]*", "<WINDOWS_PATH>", sanitized)
        sanitized = re.sub(
            r"(?<!<)/(?:[^\s/'\"]+/)+[^\s/'\"]+", "<UNIX_PATH>", sanitized
        )
        # 再过滤业务敏感内容：会话 UMO、密码/密钥类字段、提示词正文。
        sanitized = self._UMO_PATTERN.sub("<SESSION_UMO>", sanitized)
        sanitized = self._KEY_VALUE_PATTERN.sub(r"\1\2<FILTERED>", sanitized)
        sanitized = self._PROMPT_FIELD_PATTERN.sub(r"\1<FILTERED_PROMPT>", sanitized)
        return sanitized

    async def close(self) -> None:
        """关闭遥测会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("[主动消息] 遥测会话已关闭喵。")
