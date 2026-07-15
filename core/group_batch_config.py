"""群聊批次配置的校验与运行时归一化。"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


GROUP_BATCH_DEFAULTS: dict[str, Any] = {
    "batch_name": "新批次",
    "session_list": [],
    "group_idle_trigger_minutes": 30,
    "min_interval_minutes": 90,
    "max_interval_minutes": 360,
    "quiet_hours": "2-6",
    "max_unanswered_times": 2,
    "proactive_prompt": "",
}

_INTEGER_LIMITS = {
    "group_idle_trigger_minutes": (1, 1440),
    "min_interval_minutes": (1, 2880),
    "max_interval_minutes": (1, 2880),
    "max_unanswered_times": (0, 20),
}
_ALLOWED_KEYS = set(GROUP_BATCH_DEFAULTS) | {"__template_key", "template"}
_QUIET_HOURS_PATTERN = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")

SESSION_DEFAULTS = {
    "friend": {
        "min_interval_minutes": 30,
        "max_interval_minutes": 900,
        "quiet_hours": "1-7",
        "max_unanswered_times": 3,
        "group_idle_trigger_minutes": 30,
    },
    "group": {
        "min_interval_minutes": 90,
        "max_interval_minutes": 360,
        "quiet_hours": "2-6",
        "max_unanswered_times": 2,
        "group_idle_trigger_minutes": 30,
    },
}
_AUTO_TRIGGER_DEFAULTS = {
    "enable_auto_trigger": False,
    "auto_trigger_after_minutes": 5,
}
_CONTEXT_DEFAULTS = {
    "friend": {
        "source_mode": "conversation_history",
        "platform_history_count": 20,
        "platform_history_prompt": "",
        "include_bot_messages": True,
        "bot_identifiers": "bot",
    },
    "group": {
        "source_mode": "platform_message_history",
        "platform_history_count": 20,
        "platform_history_prompt": "",
        "include_bot_messages": True,
        "bot_identifiers": "bot",
    },
}
_RUNTIME_CACHE_DEFAULTS = {
    "enable": True,
    "cache_rounds": 10,
    "cache_max_chars": 4000,
    "persist_cache": False,
    "cache_storage_max_messages": 1000,
    "cache_source_policy": "cache_first",
    "runtime_cache_prompt": "",
}
_TTS_DEFAULTS = {
    "friend": {"enable_tts": True, "always_send_text": True},
    "group": {"enable_tts": False, "always_send_text": True},
}
_SEGMENTED_REPLY_DEFAULTS = {
    "enable": False,
    "words_count_threshold": 80,
    "split_mode": "regex",
    "regex": r".*?[。？！~…\n]+|.+$",
    "split_words": ["。", "？", "！", "~", "…"],
    "enable_content_cleanup": False,
    "content_cleanup_rule": r"[\n]",
    "interval_method": "log",
    "interval": "1.5, 3.5",
    "log_base": "1.8",
}


class GroupBatchValidationError(ValueError):
    """携带安全字段路径的批次配置错误，不保存原始值。"""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _fail(path: str, message: str, strict: bool) -> None:
    if strict:
        raise GroupBatchValidationError(path, message)


def _normalize_string(
    raw: Any,
    *,
    default: str,
    path: str,
    strict: bool,
) -> str:
    if raw is None:
        _fail(path, "必须是字符串", strict)
        return default
    if not isinstance(raw, str):
        _fail(path, "必须是字符串", strict)
        return default
    return raw.strip()


def _normalize_text(
    raw: Any,
    *,
    default: str,
    path: str,
    strict: bool,
) -> str:
    """校验文本但不裁剪空白，避免改变 Prompt 或正则表达式语义。"""
    if not isinstance(raw, str):
        _fail(path, "必须是字符串", strict)
        return default
    return raw


def _normalize_choice(
    raw: Any,
    *,
    default: str,
    choices: set[str],
    path: str,
    strict: bool,
) -> str:
    if not isinstance(raw, str):
        _fail(path, "必须是字符串", strict)
        return default
    value = raw.strip()
    if value not in choices:
        _fail(path, "值不在允许范围内", strict)
        return default
    return value


def _normalize_string_list(
    raw: Any,
    *,
    default: list[str],
    path: str,
    strict: bool,
) -> list[str]:
    if not isinstance(raw, list):
        _fail(path, "必须是字符串列表", strict)
        return copy.deepcopy(default)

    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            _fail(f"{path}[{index}]", "必须是非空字符串", strict)
            continue
        if item not in result:
            result.append(item)
    return result


def _normalize_regex(
    raw: Any,
    *,
    default: str,
    path: str,
    strict: bool,
) -> str:
    value = _normalize_text(raw, default=default, path=path, strict=strict)
    try:
        re.compile(value)
    except (re.error, TypeError):
        _fail(path, "必须是合法的正则表达式", strict)
        return default
    return value


def _normalize_interval(
    raw: Any,
    *,
    default: str,
    path: str,
    strict: bool,
) -> str:
    if not isinstance(raw, str):
        _fail(path, "必须是字符串", strict)
        return default
    try:
        values = [float(item.strip()) for item in raw.split(",")]
    except (TypeError, ValueError):
        values = []
    if (
        len(values) != 2
        or not all(math.isfinite(value) and value >= 0 for value in values)
        or values[0] > values[1]
    ):
        _fail(path, "格式必须是两个递增的非负数字", strict)
        return default
    return raw.strip()


def _normalize_log_base(
    raw: Any,
    *,
    default: str,
    path: str,
    strict: bool,
) -> str:
    if not isinstance(raw, str):
        _fail(path, "必须是字符串", strict)
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        value = 1.0
    if not math.isfinite(value) or value <= 1:
        _fail(path, "必须是大于 1 的数字", strict)
        return default
    return raw.strip()


def normalize_session_list(
    raw: Any,
    *,
    strict: bool = False,
    path: str = "session_list",
) -> list[str]:
    """统一处理全局配置和批次里的会话列表。

    宽容模式用于启动和运行时：空值、错误项会被忽略；严格模式用于
    Web API：只要外层或任意元素类型不对就拒绝整份提交。
    """
    if raw is None:
        _fail(path, "必须是字符串列表", strict)
        return []
    if not isinstance(raw, list):
        _fail(path, "必须是字符串列表", strict)
        return []

    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            _fail(f"{path}[{index}]", "必须是字符串", strict)
            continue
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def _normalize_context_settings(
    raw: Any,
    *,
    session_type: str,
    strict: bool,
    fill_defaults: bool,
    path: str,
) -> dict[str, Any]:
    defaults = _CONTEXT_DEFAULTS.get(session_type, _CONTEXT_DEFAULTS["friend"])
    if not isinstance(raw, dict):
        _fail(path, "必须是对象", strict)
        raw = {}
    result = copy.deepcopy(raw)

    if fill_defaults or "source_mode" in raw:
        result["source_mode"] = _normalize_choice(
            raw.get("source_mode", defaults["source_mode"]),
            default=defaults["source_mode"],
            choices={
                "conversation_history",
                "platform_message_history",
                "event_cache",
                "hybrid",
            },
            path=f"{path}.source_mode",
            strict=strict,
        )
    if fill_defaults or "platform_history_count" in raw:
        result["platform_history_count"] = _normalize_integer(
            raw.get("platform_history_count", defaults["platform_history_count"]),
            default=defaults["platform_history_count"],
            path=f"{path}.platform_history_count",
            strict=strict,
            minimum=0,
            maximum=200,
        )
    if fill_defaults or "platform_history_prompt" in raw:
        result["platform_history_prompt"] = _normalize_text(
            raw.get("platform_history_prompt", defaults["platform_history_prompt"]),
            default=defaults["platform_history_prompt"],
            path=f"{path}.platform_history_prompt",
            strict=strict,
        )
    if fill_defaults or "include_bot_messages" in raw:
        result["include_bot_messages"] = _normalize_bool(
            raw.get("include_bot_messages", defaults["include_bot_messages"]),
            default=defaults["include_bot_messages"],
            path=f"{path}.include_bot_messages",
            strict=strict,
        )
    if fill_defaults or "bot_identifiers" in raw:
        result["bot_identifiers"] = _normalize_string(
            raw.get("bot_identifiers", defaults["bot_identifiers"]),
            default=defaults["bot_identifiers"],
            path=f"{path}.bot_identifiers",
            strict=strict,
        )

    flat_aliases = {
        "enable": "runtime_cache_enable",
        "cache_rounds": "runtime_cache_rounds",
        "cache_max_chars": "runtime_cache_max_chars",
        "persist_cache": "runtime_cache_persist_cache",
        "cache_storage_max_messages": "runtime_cache_storage_max_messages",
        "cache_source_policy": "cache_source_policy",
        "runtime_cache_prompt": "runtime_cache_prompt",
    }
    has_runtime_settings = "runtime_cache_settings" in raw or any(
        alias in raw for alias in flat_aliases.values()
    )
    if fill_defaults or has_runtime_settings:
        runtime_raw = raw.get("runtime_cache_settings", {})
        runtime_path = f"{path}.runtime_cache_settings"
        if not isinstance(runtime_raw, dict):
            _fail(runtime_path, "必须是对象", strict)
            runtime_raw = {}
        runtime = copy.deepcopy(runtime_raw)

        def _runtime_value(key: str, default: Any) -> Any:
            if key in runtime_raw:
                return runtime_raw[key]
            return raw.get(flat_aliases[key], default)

        for key, default in (
            ("enable", True),
            ("persist_cache", False),
        ):
            if fill_defaults or key in runtime_raw or flat_aliases[key] in raw:
                runtime[key] = _normalize_bool(
                    _runtime_value(key, default),
                    default=default,
                    path=f"{runtime_path}.{key}",
                    strict=strict,
                )
        for key, default, minimum, maximum in (
            ("cache_rounds", 10, 0, 100),
            ("cache_max_chars", 4000, 0, 20000),
            ("cache_storage_max_messages", 1000, 50, 5000),
        ):
            if fill_defaults or key in runtime_raw or flat_aliases[key] in raw:
                runtime[key] = _normalize_integer(
                    _runtime_value(key, default),
                    default=default,
                    path=f"{runtime_path}.{key}",
                    strict=strict,
                    minimum=minimum,
                    maximum=maximum,
                )
        if (
            fill_defaults
            or "cache_source_policy" in runtime_raw
            or "cache_source_policy" in raw
        ):
            runtime["cache_source_policy"] = _normalize_choice(
                _runtime_value(
                    "cache_source_policy",
                    _RUNTIME_CACHE_DEFAULTS["cache_source_policy"],
                ),
                default=_RUNTIME_CACHE_DEFAULTS["cache_source_policy"],
                choices={
                    "cache_first",
                    "cache_only",
                    "platform_first",
                    "conversation_first",
                },
                path=f"{runtime_path}.cache_source_policy",
                strict=strict,
            )
        if (
            fill_defaults
            or "runtime_cache_prompt" in runtime_raw
            or "runtime_cache_prompt" in raw
        ):
            runtime["runtime_cache_prompt"] = _normalize_text(
                _runtime_value(
                    "runtime_cache_prompt",
                    _RUNTIME_CACHE_DEFAULTS["runtime_cache_prompt"],
                ),
                default=_RUNTIME_CACHE_DEFAULTS["runtime_cache_prompt"],
                path=f"{runtime_path}.runtime_cache_prompt",
                strict=strict,
            )
        result["runtime_cache_settings"] = runtime

    if "platform_context_max_chars" in raw:
        result["platform_context_max_chars"] = _normalize_integer(
            raw["platform_context_max_chars"],
            default=4000,
            path=f"{path}.platform_context_max_chars",
            strict=strict,
            minimum=0,
            maximum=20000,
        )

    return result


def _normalize_tts_settings(
    raw: Any,
    *,
    session_type: str,
    strict: bool,
    fill_defaults: bool,
    path: str,
) -> dict[str, Any]:
    defaults = _TTS_DEFAULTS.get(session_type, _TTS_DEFAULTS["friend"])
    if not isinstance(raw, dict):
        _fail(path, "必须是对象", strict)
        raw = {}
    result = copy.deepcopy(raw)
    for key, default in defaults.items():
        if fill_defaults or key in raw:
            result[key] = _normalize_bool(
                raw.get(key, default),
                default=default,
                path=f"{path}.{key}",
                strict=strict,
            )
    return result


def _normalize_segmented_reply_settings(
    raw: Any,
    *,
    strict: bool,
    fill_defaults: bool,
    path: str,
) -> dict[str, Any]:
    defaults = _SEGMENTED_REPLY_DEFAULTS
    if not isinstance(raw, dict):
        _fail(path, "必须是对象", strict)
        raw = {}
    result = copy.deepcopy(raw)

    for key in ("enable", "enable_content_cleanup"):
        if fill_defaults or key in raw:
            result[key] = _normalize_bool(
                raw.get(key, defaults[key]),
                default=defaults[key],
                path=f"{path}.{key}",
                strict=strict,
            )
    if fill_defaults or "words_count_threshold" in raw:
        result["words_count_threshold"] = _normalize_integer(
            raw.get("words_count_threshold", defaults["words_count_threshold"]),
            default=defaults["words_count_threshold"],
            path=f"{path}.words_count_threshold",
            strict=strict,
            minimum=0,
            maximum=1024,
        )
    for key, choices in (
        ("split_mode", {"regex", "words"}),
        ("interval_method", {"random", "log"}),
    ):
        if fill_defaults or key in raw:
            result[key] = _normalize_choice(
                raw.get(key, defaults[key]),
                default=defaults[key],
                choices=choices,
                path=f"{path}.{key}",
                strict=strict,
            )
    for key in ("regex", "content_cleanup_rule"):
        if fill_defaults or key in raw:
            result[key] = _normalize_regex(
                raw.get(key, defaults[key]),
                default=defaults[key],
                path=f"{path}.{key}",
                strict=strict,
            )
    if fill_defaults or "split_words" in raw:
        result["split_words"] = _normalize_string_list(
            raw.get("split_words", defaults["split_words"]),
            default=defaults["split_words"],
            path=f"{path}.split_words",
            strict=strict,
        )
    if fill_defaults or "interval" in raw:
        result["interval"] = _normalize_interval(
            raw.get("interval", defaults["interval"]),
            default=defaults["interval"],
            path=f"{path}.interval",
            strict=strict,
        )
    if fill_defaults or "log_base" in raw:
        result["log_base"] = _normalize_log_base(
            raw.get("log_base", defaults["log_base"]),
            default=defaults["log_base"],
            path=f"{path}.log_base",
            strict=strict,
        )
    return result


def _normalize_integer(
    raw: Any,
    *,
    default: int,
    path: str,
    strict: bool,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        _fail(path, "必须是整数", strict)
        return default
    if raw < minimum or raw > maximum:
        _fail(path, f"必须在 {minimum} 到 {maximum} 之间", strict)
        return default
    return raw


def _normalize_bool(raw: Any, *, default: bool, path: str, strict: bool) -> bool:
    if not isinstance(raw, bool):
        _fail(path, "必须是布尔值", strict)
        return default
    return raw


def _normalize_quiet_hours(raw: Any, *, default: str, path: str, strict: bool) -> str:
    value = _normalize_string(raw, default=default, path=path, strict=strict)
    match = _QUIET_HOURS_PATTERN.fullmatch(value)
    if not match or not all(0 <= int(item) <= 23 for item in match.groups()):
        _fail(path, "格式必须是 0-23-0-23 的时间段", strict)
        return default
    return f"{int(match.group(1))}-{int(match.group(2))}"


def normalize_session_settings(
    raw: Any,
    *,
    session_type: str,
    strict: bool = False,
    fill_defaults: bool = True,
    path: str = "settings",
) -> dict[str, Any]:
    """归一化私聊/群聊全局配置及会话覆写中的已知字段。

    严格模式用于 API，宽容模式用于运行时。未识别的旧字段原样保留，
    已知字段则校验类型并在宽容模式下回退安全默认值。
    """
    if not isinstance(raw, dict):
        _fail(path, "必须是对象", strict)
        return {}

    defaults = SESSION_DEFAULTS.get(session_type, SESSION_DEFAULTS["friend"])
    result = copy.deepcopy(raw)

    if fill_defaults or "enable" in raw:
        result["enable"] = _normalize_bool(
            raw.get("enable", False),
            default=False,
            path=f"{path}.enable",
            strict=strict,
        )
    if fill_defaults or "session_list" in raw:
        result["session_list"] = normalize_session_list(
            raw.get("session_list", []),
            strict=strict,
            path=f"{path}.session_list",
        )

    if fill_defaults or "auto_trigger_settings" in raw:
        raw_auto = raw.get("auto_trigger_settings", {})
        if not isinstance(raw_auto, dict):
            _fail(f"{path}.auto_trigger_settings", "必须是对象", strict)
            raw_auto = {}
        auto = copy.deepcopy(raw_auto)
        auto["enable_auto_trigger"] = _normalize_bool(
            raw_auto.get(
                "enable_auto_trigger", _AUTO_TRIGGER_DEFAULTS["enable_auto_trigger"]
            ),
            default=False,
            path=f"{path}.auto_trigger_settings.enable_auto_trigger",
            strict=strict,
        )
        auto["auto_trigger_after_minutes"] = _normalize_integer(
            raw_auto.get(
                "auto_trigger_after_minutes",
                _AUTO_TRIGGER_DEFAULTS["auto_trigger_after_minutes"],
            ),
            default=5,
            path=f"{path}.auto_trigger_settings.auto_trigger_after_minutes",
            strict=strict,
            minimum=1,
            maximum=1440,
        )
        result["auto_trigger_settings"] = auto

    if fill_defaults or "schedule_settings" in raw:
        raw_schedule = raw.get("schedule_settings", {})
        if not isinstance(raw_schedule, dict):
            _fail(f"{path}.schedule_settings", "必须是对象", strict)
            raw_schedule = {}
        schedule = copy.deepcopy(raw_schedule)
        for key, minimum, maximum in (
            ("min_interval_minutes", 1, 2880),
            ("max_interval_minutes", 1, 2880),
            ("max_unanswered_times", 0, 20),
        ):
            if fill_defaults or key in raw_schedule:
                schedule[key] = _normalize_integer(
                    raw_schedule.get(key, defaults[key]),
                    default=defaults[key],
                    path=f"{path}.schedule_settings.{key}",
                    strict=strict,
                    minimum=minimum,
                    maximum=maximum,
                )
        if fill_defaults or "quiet_hours" in raw_schedule:
            schedule["quiet_hours"] = _normalize_quiet_hours(
                raw_schedule.get("quiet_hours", defaults["quiet_hours"]),
                default=defaults["quiet_hours"],
                path=f"{path}.schedule_settings.quiet_hours",
                strict=strict,
            )
        if (
            "min_interval_minutes" in schedule
            and "max_interval_minutes" in schedule
            and schedule["min_interval_minutes"] > schedule["max_interval_minutes"]
        ):
            _fail(
                f"{path}.schedule_settings",
                "最小触发间隔不能大于最大触发间隔",
                strict,
            )
            schedule["max_interval_minutes"] = schedule["min_interval_minutes"]
        result["schedule_settings"] = schedule

    if session_type == "group" and (
        fill_defaults or "group_idle_trigger_minutes" in raw
    ):
        result["group_idle_trigger_minutes"] = _normalize_integer(
            raw.get(
                "group_idle_trigger_minutes", defaults["group_idle_trigger_minutes"]
            ),
            default=defaults["group_idle_trigger_minutes"],
            path=f"{path}.group_idle_trigger_minutes",
            strict=strict,
            minimum=1,
            maximum=1440,
        )

    if fill_defaults or "proactive_prompt" in raw:
        result["proactive_prompt"] = _normalize_string(
            raw.get("proactive_prompt", ""),
            default="",
            path=f"{path}.proactive_prompt",
            strict=strict,
        )

    if fill_defaults or "context_settings" in raw:
        result["context_settings"] = _normalize_context_settings(
            raw.get("context_settings", {}),
            session_type=session_type,
            strict=strict,
            fill_defaults=fill_defaults,
            path=f"{path}.context_settings",
        )

    if fill_defaults or "tts_settings" in raw:
        result["tts_settings"] = _normalize_tts_settings(
            raw.get("tts_settings", {}),
            session_type=session_type,
            strict=strict,
            fill_defaults=fill_defaults,
            path=f"{path}.tts_settings",
        )

    if fill_defaults or "segmented_reply_settings" in raw:
        result["segmented_reply_settings"] = _normalize_segmented_reply_settings(
            raw.get("segmented_reply_settings", {}),
            strict=strict,
            fill_defaults=fill_defaults,
            path=f"{path}.segmented_reply_settings",
        )

    return result


def normalize_group_batch(
    raw: Any,
    *,
    index: int,
    strict: bool = False,
    add_template_key: bool = False,
    fill_defaults: bool = True,
) -> dict[str, Any] | None:
    """归一化一个批次；宽容模式下坏项返回 None，不向外抛异常。"""
    path = f"group_batches[{index}]"
    if not isinstance(raw, dict):
        _fail(path, "必须是对象", strict)
        return None

    unknown_keys = set(raw) - _ALLOWED_KEYS
    if unknown_keys and strict:
        raise GroupBatchValidationError(path, "包含不支持的字段")

    result: dict[str, Any] = {}
    result["batch_name"] = (
        _normalize_string(
            raw.get("batch_name", GROUP_BATCH_DEFAULTS["batch_name"]),
            default=GROUP_BATCH_DEFAULTS["batch_name"],
            path=f"{path}.batch_name",
            strict=strict,
        )
        or GROUP_BATCH_DEFAULTS["batch_name"]
    )

    result["session_list"] = normalize_session_list(
        raw.get("session_list", GROUP_BATCH_DEFAULTS["session_list"]),
        strict=strict,
        path=f"{path}.session_list",
    )

    for key, (minimum, maximum) in _INTEGER_LIMITS.items():
        if fill_defaults or key in raw:
            result[key] = _normalize_integer(
                raw.get(key, GROUP_BATCH_DEFAULTS[key]),
                default=GROUP_BATCH_DEFAULTS[key],
                path=f"{path}.{key}",
                strict=strict,
                minimum=minimum,
                maximum=maximum,
            )

    if (
        "min_interval_minutes" in result
        and "max_interval_minutes" in result
        and result["min_interval_minutes"] > result["max_interval_minutes"]
    ):
        _fail(
            path,
            "最小触发间隔不能大于最大触发间隔",
            strict,
        )
        result["max_interval_minutes"] = result["min_interval_minutes"]

    if fill_defaults or "quiet_hours" in raw:
        quiet_hours = raw.get("quiet_hours", GROUP_BATCH_DEFAULTS["quiet_hours"])
        quiet_hours = _normalize_string(
            quiet_hours,
            default=GROUP_BATCH_DEFAULTS["quiet_hours"],
            path=f"{path}.quiet_hours",
            strict=strict,
        )
        quiet_match = _QUIET_HOURS_PATTERN.fullmatch(quiet_hours)
        if quiet_match and all(0 <= int(value) <= 23 for value in quiet_match.groups()):
            result["quiet_hours"] = (
                f"{int(quiet_match.group(1))}-{int(quiet_match.group(2))}"
            )
        else:
            _fail(f"{path}.quiet_hours", "格式必须是 0-23-0-23 的时间段", strict)
            result["quiet_hours"] = GROUP_BATCH_DEFAULTS["quiet_hours"]

    if fill_defaults or "proactive_prompt" in raw:
        result["proactive_prompt"] = _normalize_string(
            raw.get("proactive_prompt", GROUP_BATCH_DEFAULTS["proactive_prompt"]),
            default=GROUP_BATCH_DEFAULTS["proactive_prompt"],
            path=f"{path}.proactive_prompt",
            strict=strict,
        )
    if add_template_key:
        result["__template_key"] = "group_batch"
    elif raw.get("__template_key") == "group_batch":
        result["__template_key"] = "group_batch"
    return result


def normalize_group_batches(
    raw: Any,
    *,
    strict: bool = False,
    add_template_key: bool = False,
    fill_defaults: bool = True,
) -> list[dict[str, Any]]:
    """归一化批次列表；严格模式用于 API，宽容模式用于运行时。"""
    if not isinstance(raw, list):
        _fail("group_batches", "必须是对象列表", strict)
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        batch = normalize_group_batch(
            item,
            index=index,
            strict=strict,
            add_template_key=add_template_key,
            fill_defaults=fill_defaults,
        )
        if batch is not None:
            normalized.append(batch)
    return normalized
