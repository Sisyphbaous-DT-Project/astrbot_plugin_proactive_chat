"""不携带异常原文的安全日志辅助函数。"""

from __future__ import annotations

import builtins
from typing import Any


_SAFE_BUILTIN_EXCEPTION_TYPES = tuple(
    (value, name)
    for name, value in vars(builtins).items()
    if type(value) is type and issubclass(value, BaseException)
)

_SAFE_ERROR_CODES = frozenset(
    {
        "PC-ASYNC-001",
        "PC-ASYNC-002",
        "PC-ASYNC-003",
        "PC-ASYNC-004",
        "PC-ASYNC-005",
        "PC-CACHE-001",
        "PC-CACHE-002",
        "PC-CACHE-003",
        "PC-CACHE-004",
        "PC-CONFIG-001",
        "PC-CONV-002",
        "PC-EVENT-001",
        "PC-EVENT-002",
        "PC-EVENT-003",
        "PC-HISTORY-001",
        "PC-CHAT-001",
        "PC-CHAT-002",
        "PC-CHAT-003",
        "PC-CHAT-004",
        "PC-CHAT-005",
        "PC-LLM-001",
        "PC-LLM-002",
        "PC-LLM-003",
        "PC-LLM-004",
        "PC-OVERRIDE-001",
        "PC-OVERRIDE-002",
        "PC-SEND-001",
        "PC-STORAGE-001",
        "PC-STORAGE-002",
        "PC-UNKNOWN",
        "PC-VERSION-001",
        "PC-VERSION-002",
        "PC-VERSION-003",
        "PC-VERSION-004",
        "PC-VERSION-005",
        "PC-WEB-001",
        "PC-WEB-002",
        "PC-WEB-003",
        "PC-WEB-004",
        "PC-WEB-005",
        "PC-WEB-006",
        "PC-WEB-007",
        "PC-WEB-008",
        "PC-WEB-009",
        "PC-WEB-010",
        "PC-WEB-011",
        "PC-WEB-012",
        "PC-WEB-013",
        "PC-WEB-INIT",
    }
    | {f"PC-SEND-{index:03d}" for index in range(1, 13)}
    | {f"PC-LIFECYCLE-{index:03d}" for index in range(16)}
    | {f"PC-NOTIFY-{index:03d}" for index in range(1, 6)}
    | {f"PC-SCHEDULER-{index:03d}" for index in range(1, 9)}
)


def exception_type_name(error: BaseException | None) -> str:
    """只返回异常类型，避免把第三方异常消息或堆栈写入日志。"""
    if error is None:
        return "UnknownError"
    try:
        error_type = type(error)
        for safe_type, safe_name in _SAFE_BUILTIN_EXCEPTION_TYPES:
            if error_type is safe_type:
                return safe_name
    except BaseException:
        # 第三方异常类型的元类也可能在比较过程中抛错，安全日志不能反过来
        # 触发原始异常文本泄露。
        return "ExternalError"
    return "ExternalError"


def safe_error_code(code: str) -> str:
    """只允许固定格式的错误编号，避免把动态文本拼进日志。"""
    return code if type(code) is str and code in _SAFE_ERROR_CODES else "PC-UNKNOWN"


def log_safe_exception(
    logger: Any,
    level: str,
    code: str,
    message: str,
    error: BaseException | None = None,
) -> None:
    """只记录固定错误编号和安全异常类型，不读取动态说明或异常文本。"""
    log_method = getattr(logger, level, None)
    if not callable(log_method):
        return
    try:
        error_type = exception_type_name(error)
    except BaseException:
        error_type = "ExternalError"
    suffix = f"，错误类型: {error_type}" if error is not None else ""
    del message
    log_method(f"[主动消息][{safe_error_code(code)}]{suffix}。")
