"""主动消息插件 Web 管理端服务。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    # Web 管理端完全基于 FastAPI / Uvicorn 提供 HTTP 与 WebSocket 能力。
    import uvicorn
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    FASTAPI_AVAILABLE = True
except ImportError:
    # 允许插件主体在缺少 FastAPI 依赖时继续工作，只是禁用 Web 控制台。
    FASTAPI_AVAILABLE = False
    logger.warning(
        "[主动消息] FastAPI 未安装，Web 管理端不可用。请安装: pip install fastapi uvicorn"
    )


def _is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker / 容器环境中。"""
    if os.path.exists("/.dockerenv"):
        return True

    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
            if "/docker/" in content or "/kubepods/" in content:
                return True
    except Exception:
        pass

    return os.environ.get("DOCKER_CONTAINER") == "true"


class WebAdminServer:
    """主动消息插件 Web 管理端服务器。"""

    def __init__(self, plugin: Any):
        # plugin 是主插件实例，Web 端所有状态与操作都通过它间接访问。
        self.plugin = plugin
        # 直接缓存配置引用，便于路由中统一读写。
        self.config = plugin.config
        # FastAPI 应用实例，仅在依赖存在时初始化。
        self.app: FastAPI | None = None
        # Uvicorn Server 实例，用于控制启动与停止。
        self.server = None
        # 后台运行的 serve 任务，stop 时需要等待其退出。
        self.server_task: asyncio.Task | None = None
        # 定时清理过期 token 的后台任务。
        self._token_cleanup_task: asyncio.Task | None = None
        # 当前已建立的 WebSocket 连接列表，用于广播 UI 更新。
        self._ws_connections: list[WebSocket] = []
        # 登录令牌默认有效期 24 小时。
        self._token_expire_seconds = 60 * 60 * 24
        # 简单的内存令牌表：token -> 过期时间戳。
        self._tokens: dict[str, float] = {}
        # 仅当配置中设置了密码时才开启鉴权。
        self._auth_enabled = bool(self.config.get("web_admin", {}).get("password", ""))

        if FASTAPI_AVAILABLE:
            # 只有环境具备依赖时才构建 Web 应用，避免 import 失败影响插件主体。
            self._setup_app()

    def _setup_app(self) -> None:
        # 创建 FastAPI 应用，版本号用于控制台元信息展示。
        self.app = FastAPI(
            title="主动消息管理端",
            description="主动消息插件独立 WebUI",
        )

        # 管理端通常运行在本地独立端口；在允许凭据时使用显式本地来源列表，避免 "*" 带来的安全/兼容问题。
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:4100",
                "http://127.0.0.1:4100",
                "http://localhost",
                "http://127.0.0.1",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            # 未启用密码保护时，所有请求直接放行。
            if not self._auth_enabled:
                return await call_next(request)

            path = request.url.path
            # 登录接口与鉴权信息探测接口必须允许匿名访问，否则前端无法完成登录。
            if path in {"/api/login", "/api/auth-info"}:
                return await call_next(request)

            # 非 API 路径主要是静态文件，不在这里拦截，前端自行处理启动页逻辑。
            if not path.startswith("/api"):
                return await call_next(request)

            # API 请求统一使用 Bearer Token 认证。
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse({"error": "未授权"}, status_code=401)

            token = auth_header[7:]
            # 令牌不存在、已过期或不合法时，返回 401 让前端重新登录。
            if not self._verify_token(token):
                return JSONResponse({"error": "登录已过期"}, status_code=401)

            return await call_next(request)

        # 路由与静态资源挂载分开处理，方便后续维护。
        self._register_routes()
        self._mount_static_files()

    def _mount_static_files(self) -> None:
        if not self.app:
            return

        # admin 目录位于插件根目录下，是整个前端控制台的静态资源根路径。
        admin_dir = Path(__file__).resolve().parent.parent / "admin"
        if admin_dir.exists():
            # 将根路径直接挂到静态文件目录，便于通过 / 访问前端页面。
            self.app.mount(
                "/", StaticFiles(directory=str(admin_dir), html=True), name="admin"
            )
        else:
            logger.warning(f"[主动消息] 未找到管理端静态目录喵: {admin_dir}")

    def _register_routes(self) -> None:
        if not self.app:
            return

        @self.app.get("/api/auth-info")
        async def auth_info():
            # 前端启动时会先调用该接口，判断是否需要展示登录流程。
            return {"auth_required": self._auth_enabled}

        @self.app.post("/api/login")
        async def login(credentials: dict[str, Any]):
            # 从配置中读取管理端密码；未配置密码时视为关闭鉴权。
            password = self.config.get("web_admin", {}).get("password", "")
            if not password:
                # 返回固定 no-auth token，便于前端保持统一的请求头处理逻辑。
                return {"token": "no-auth", "auth_required": False}

            input_password = str(credentials.get("password", ""))
            # 使用常量时间比较，避免简单的时序侧信道问题。
            if not secrets.compare_digest(input_password, password):
                return JSONResponse({"error": "密码错误"}, status_code=401)

            token = self._issue_token()
            return {"token": token, "auth_required": True}

        @self.app.get("/logo.png")
        async def get_logo():
            # 兼容前端在不同相对路径下请求 logo 的场景。
            logo_path = Path(__file__).resolve().parent.parent / "logo.png"
            if logo_path.exists():
                return FileResponse(str(logo_path), media_type="image/png")
            return JSONResponse({"error": "logo not found"}, status_code=404)

        @self.app.get("/api/status")
        async def get_status():
            # 汇总插件运行状态、计时器与连接数，供首页卡片与轮询逻辑使用。
            return self._build_status_payload()

        @self.app.get("/api/config")
        async def get_config():
            # 返回配置时显式过滤密码字段，避免管理端读取到明文密码。
            web_admin = {
                k: v
                for k, v in self.config.get("web_admin", {}).items()
                if k != "password"
            }
            return {
                "friend_settings": dict(self.config.get("friend_settings", {})),
                "group_settings": dict(self.config.get("group_settings", {})),
                "web_admin": web_admin,
            }

        @self.app.get("/api/config-schema")
        async def get_config_schema():
            # Schema 用于前端动态渲染配置表单，而不是写死表单结构。
            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            if schema_path.exists():
                try:
                    schema_text = await asyncio.to_thread(
                        schema_path.read_text, encoding="utf-8"
                    )
                    return json.loads(schema_text)
                except Exception as e:
                    logger.error(f"[主动消息] 读取 Schema 失败: {e}")
            return {}

        @self.app.post("/api/config")
        async def update_config(payload: dict[str, Any]):
            # 仅允许更新这三个一级配置块，避免前端误写其它未知字段。
            allowed_keys = {"friend_settings", "group_settings", "web_admin"}
            for key in allowed_keys:
                if key not in payload:
                    continue
                if key == "web_admin":
                    # web_admin 采用增量合并，避免未提交字段被整个覆盖掉。
                    old = dict(self.config.get("web_admin", {}))
                    old.update(payload.get("web_admin", {}))
                    # 密码字段允许显式更新，但不会通过 get_config 回传给前端。
                    if "password" in payload.get("web_admin", {}):
                        old["password"] = payload["web_admin"]["password"]
                    self.config["web_admin"] = old
                else:
                    # friend / group 配置块按前端提交的完整对象直接替换。
                    self.config[key] = payload[key]

            self._save_plugin_config()
            # 配置变更后立即广播，确保所有已打开页面实时刷新。
            await self._broadcast_update("config")
            return {"ok": True}

        @self.app.get("/api/session-config/sessions")
        async def list_session_configs():
            # 汇总所有已知会话，给前端会话差异配置页做选择器与列表展示。
            sessions = self._list_known_sessions()
            result = []
            for session in sessions:
                override = self.plugin.session_override_manager.get_override(session)
                effective = self.plugin._get_session_config(session)
                session_name = self.plugin._get_session_name(session, effective)
                result.append(
                    {
                        "session": session,
                        "session_name": session_name,
                        "session_display_name": self.plugin._get_session_display_name(
                            session, effective
                        ),
                        # 标记是否存在会话级覆写，前端可据此展示提示标签。
                        "has_override": bool(override),
                        "override_keys": list(override.keys()),
                        # effective 可能为空，因此这里需要防御式布尔判断。
                        "enabled": bool(effective and effective.get("enable", False)),
                        # 从运行时会话数据中拿到下一次触发时间，用于列表辅助信息展示。
                        "next_trigger_time": self.plugin.session_data.get(
                            session, {}
                        ).get("next_trigger_time"),
                        "unanswered_count": self.plugin.session_data.get(
                            session, {}
                        ).get("unanswered_count", 0),
                    }
                )
            return {"sessions": result}

        @self.app.get("/api/session-config/{umo:path}")
        async def get_session_config(umo: str):
            # 路径参数使用 path 转换器，允许会话 ID 中包含斜杠等特殊字符。
            normalized = self.plugin._normalize_session_id(umo)
            base = self.plugin._get_base_session_config(normalized)
            return {
                "session": normalized,
                # base 表示命中 friend/group 全局配置后的基础配置。
                "base": base,
                # override 是该会话显式保存的差异字段。
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                # effective 是基础配置与覆写合并后的最终生效配置。
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.post("/api/session-config/{umo:path}")
        async def update_session_config(umo: str, payload: dict[str, Any]):
            normalized = self.plugin._normalize_session_id(umo)
            # mode 用于兼容两种写法：直接提交 override，或提交最终 effective 配置。
            mode = payload.get("mode", "effective")

            if mode == "override":
                override = payload.get("override", {})
                if not isinstance(override, dict):
                    return JSONResponse(
                        {"error": "override 必须是对象"}, status_code=400
                    )
                await self.plugin.session_override_manager.set_override(
                    normalized, override
                )
            else:
                effective = payload.get("effective", {})
                if not isinstance(effective, dict):
                    return JSONResponse(
                        {"error": "effective 必须是对象"}, status_code=400
                    )
                base = self.plugin._get_base_session_config(normalized)
                if not base:
                    # 没有基础配置时无法反推出差异项，因此拒绝保存 effective。
                    return JSONResponse(
                        {
                            "error": "会话未命中 friend/group 全局配置，无法保存 effective"
                        },
                        status_code=400,
                    )
                await (
                    self.plugin.session_override_manager.update_session_from_effective(
                        normalized,
                        base,
                        effective,
                    )
                )

            await self._broadcast_update("session-config")
            return {
                "ok": True,
                "session": normalized,
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.delete("/api/session-config/{umo:path}")
        async def reset_session_config(umo: str):
            # 删除覆写后，会话会重新完全继承全局配置。
            normalized = self.plugin._normalize_session_id(umo)
            await self.plugin.session_override_manager.delete_override(normalized)
            await self._broadcast_update("session-config")
            return {
                "ok": True,
                "session": normalized,
                "override": {},
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.get("/api/jobs")
        async def list_jobs():
            # 返回调度器中的待执行任务列表，供任务页卡片展示。
            return {"jobs": self._collect_jobs()}

        @self.app.post("/api/open-directory")
        async def open_directory(payload: dict[str, Any]):
            # 允许前端请求打开插件目录或数据目录，便于管理员快速定位文件。
            target = str(payload.get("path", "plugin")).strip().lower()
            if target == "data":
                directory = Path(self.plugin.data_dir)
            else:
                directory = Path(__file__).resolve().parent.parent

            try:
                # 确保目录存在，再根据当前系统选择合适的打开方式。
                directory.mkdir(parents=True, exist_ok=True)
                dir_str = str(directory)

                if _is_running_in_docker():
                    return JSONResponse(
                        {
                            "error": "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径",
                            "path": dir_str,
                        },
                        status_code=400,
                    )

                if os.name == "nt":
                    # Windows 使用系统默认资源管理器，封装为异步避免阻塞事件循环。
                    await asyncio.to_thread(os.startfile, dir_str)
                elif sys.platform == "darwin":
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（macOS）",
                                "message": f"open 命令执行失败: {detail}",
                                "path": dir_str,
                            },
                            status_code=500,
                        )
                else:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["xdg-open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（Linux）",
                                "message": (
                                    "xdg-open 执行失败，服务器可能缺少桌面环境或未安装 xdg-open: "
                                    f"{detail}"
                                ),
                                "path": dir_str,
                            },
                            status_code=500,
                        )

                return {
                    "ok": True,
                    "path": dir_str,
                    "message": "已在系统文件管理器中打开目录",
                }
            except FileNotFoundError as e:
                logger.error(f"[主动消息] 打开目录失败（命令缺失）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：系统缺少所需命令",
                        "message": "请确认系统已安装对应文件管理器命令（如 open / xdg-open）",
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except PermissionError as e:
                logger.error(f"[主动消息] 打开目录失败（权限不足）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：权限不足",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except Exception as e:
                logger.error(f"[主动消息] 打开目录失败喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )

        @self.app.post("/api/jobs/{umo:path}/trigger")
        async def trigger_job(umo: str):
            # 立即手动触发一次指定会话的检查与发言流程，不阻塞当前请求。
            normalized = self.plugin._normalize_session_id(umo)
            asyncio.create_task(self.plugin.check_and_chat(normalized))
            await self._broadcast_update("jobs")
            return {"ok": True, "session": normalized}

        @self.app.delete("/api/jobs/{umo:path}")
        async def cancel_job(umo: str):
            normalized = self.plugin._normalize_session_id(umo)
            removed = False
            try:
                # APScheduler 中的 job id 直接使用规范化后的 session id。
                self.plugin.scheduler.remove_job(normalized)
                removed = True
            except Exception:
                # 任务不存在时保持幂等，不把异常直接抛给前端。
                pass

            async with self.plugin.data_lock:
                if normalized in self.plugin.session_data:
                    # 同步清理持久化数据中的 next_trigger_time，避免界面显示过期信息。
                    self.plugin.session_data[normalized].pop("next_trigger_time", None)
                    await self.plugin._save_data_internal()

            await self._broadcast_update("jobs")
            return {"ok": True, "session": normalized, "removed": removed}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            if self._auth_enabled:
                # WebSocket 无法沿用普通中间件，这里单独做一次 token 校验。
                token = websocket.query_params.get("token", "")
                if not token:
                    auth_header = websocket.headers.get("Authorization", "")
                    if auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                if not self._verify_token(token):
                    # 1008 表示策略违规，适合表达认证失败。
                    await websocket.close(code=1008)
                    return

            await websocket.accept()
            self._ws_connections.append(websocket)

            try:
                # 连接建立后先推送一次完整快照，避免前端依赖额外首次拉取。
                await websocket.send_json(
                    {
                        "type": "full_update",
                        "data": {
                            "status": self._build_status_payload(),
                            "jobs": self._collect_jobs(),
                            "sessions": self._list_known_session_summaries(),
                        },
                    }
                )

                while True:
                    # 前端只需发送轻量消息：ping 保活、refresh 主动请求全量刷新。
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    msg_type = msg.get("type")
                    if msg_type == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg_type == "refresh":
                        await websocket.send_json(
                            {
                                "type": "full_update",
                                "data": {
                                    "status": self._build_status_payload(),
                                    "jobs": self._collect_jobs(),
                                    "sessions": self._list_known_session_summaries(),
                                },
                            }
                        )
            except WebSocketDisconnect:
                # 浏览器主动关闭标签页时会进入这里，属于正常流程。
                pass
            except Exception as e:
                logger.debug(f"[主动消息] WebSocket 连接异常喵: {e}")
            finally:
                # 无论异常还是正常断开，都必须回收连接引用，避免广播时残留死连接。
                if websocket in self._ws_connections:
                    self._ws_connections.remove(websocket)

    def _save_plugin_config(self) -> None:
        try:
            # AstrBot 配置对象通常提供 save_config 方法，这里做鸭子类型兼容。
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[主动消息] 保存配置失败喵: {e}")

    def _issue_token(self) -> str:
        # 生成适合放入 URL / Header 的安全随机 token。
        token = secrets.token_urlsafe(24)
        self._tokens[token] = time.time() + self._token_expire_seconds
        return token

    def _verify_token(self, token: str) -> bool:
        if not token:
            return False
        if token == "no-auth":
            # 在未启用鉴权时允许该哨兵令牌直接通过。
            return True
        expire_at = self._tokens.get(token)
        if not expire_at:
            return False
        if time.time() > expire_at:
            # 过期即顺手删除，避免内存令牌表无限增长。
            self._tokens.pop(token, None)
            return False
        return True

    def _safe_timer_meta(self, timer: Any, now: float) -> dict[str, float | int | None]:
        if timer is None:
            return {"remaining_seconds": None, "target_time": None}

        try:
            # 某些定时句柄可能已取消；这里优先过滤掉不可用状态。
            if getattr(timer, "cancelled", lambda: False)():
                return {"remaining_seconds": None, "target_time": None}
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

        # asyncio 定时句柄通常暴露 when() 方法，返回 loop 单调时钟上的目标时刻。
        when_method = getattr(timer, "when", None)
        if not callable(when_method):
            return {"remaining_seconds": None, "target_time": None}

        try:
            loop_time = when_method()
            loop = getattr(timer, "_loop", None)
            current_loop_time = loop.time() if loop else None
            if current_loop_time is None:
                return {"remaining_seconds": None, "target_time": None}

            # 用单调时钟差值推导剩余秒数，再换算成当前 Unix 时间戳，避免受系统时间跳变影响。
            remaining_precise = max(0.0, loop_time - current_loop_time)
            target_time = now + remaining_precise
            return {
                # 向上取整，保证 UI 倒计时不会过早显示为 0。
                "remaining_seconds": max(0, int(math.ceil(remaining_precise))),
                "target_time": target_time,
            }
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

    def _detect_session_category(self, session_id: str) -> str:
        # 优先使用插件已有解析逻辑识别会话类型，避免前后端规则不一致。
        parsed = self.plugin._parse_session_id(session_id)
        if not parsed:
            lowered = str(session_id).lower()
            return "group" if "group" in lowered else "friend"

        _, msg_type, _ = parsed
        return "group" if "group" in msg_type.lower() else "friend"

    def _collect_timer_cards(self, now: float) -> dict[str, list[dict[str, Any]]]:
        # auto_cards：自动触发检测计时器；group_cards：群沉默计时器。
        auto_cards: list[dict[str, Any]] = []
        group_cards: list[dict[str, Any]] = []
        # 群计时器优先展示为 group_silence，避免同一群会话被重复渲染两种卡片。
        active_group_sessions = {
            str(session_id) for session_id in self.plugin.group_timers.keys()
        }

        for session_id, timer in list(self.plugin.auto_trigger_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            if normalized_session_id in active_group_sessions:
                continue

            session_config = self.plugin._get_session_config(session_id) or {}
            session_data = self.plugin.session_data.get(session_id, {})
            auto_settings = session_config.get("auto_trigger_settings", {})
            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            trigger_delay_seconds = max(0, trigger_delay_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 若拿不到真实开始时间，则退化为“插件启动时间”或根据窗口长度反推一个近似值。
            started_at = max(self.plugin.plugin_start_time, now - trigger_delay_seconds)
            progress_percent = 0
            if trigger_delay_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, trigger_delay_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / trigger_delay_seconds) * 100))
                )

            auto_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at,
                    "window_seconds": trigger_delay_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                }
            )

        for session_id, timer in list(self.plugin.group_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            session_config = (
                self.plugin._get_session_config(normalized_session_id) or {}
            )
            session_data = self.plugin.session_data.get(normalized_session_id, {})
            idle_minutes = int(session_config.get("group_idle_trigger_minutes", 0) or 0)
            idle_seconds = max(0, idle_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 群沉默计时器更适合以“最后一条用户消息时间”作为窗口起点。
            last_message_time = self.plugin.last_message_times.get(
                normalized_session_id, 0
            )
            temp_state = self.plugin.session_temp_state.get(normalized_session_id, {})
            last_user_time = (
                temp_state.get("last_user_time") or last_message_time or None
            )
            # 若历史时间缺失，则根据剩余时间反推一个近似 started_at。
            started_at = last_user_time or (
                now - max(0, idle_seconds - (remaining_seconds or 0))
            )
            progress_percent = 0
            if idle_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, idle_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / idle_seconds) * 100))
                )

            group_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "timer_kind": "group_silence",
                    "title": "群沉默检测",
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at if started_at else None,
                    "window_seconds": idle_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "group_idle_trigger_minutes": idle_minutes,
                    "last_message_time": last_message_time or None,
                    "last_user_time": last_user_time,
                    # 显式标记这是实时群计时器，便于前端做差异化展示或调试。
                    "is_live_group_timer": True,
                }
            )

        # 统一按剩余时间升序排序，让最接近触发的卡片优先显示。
        auto_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        group_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        return {
            "auto_trigger_cards": auto_cards,
            "group_timer_cards": group_cards,
        }

    def _build_status_payload(self) -> dict[str, Any]:
        now = time.time()
        uptime_sec = max(0, int(now - self.plugin.plugin_start_time))
        metadata_version = None
        try:
            # 优先从 metadata.yaml 读取版本，兼容插件实例未显式暴露 version 属性的情况。
            metadata_path = Path(__file__).resolve().parent.parent / "metadata.yaml"
            if metadata_path.exists():
                for line in metadata_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("version:"):
                        metadata_version = (
                            stripped.split(":", 1)[1].strip().strip('"').strip("'")
                        )
                        break
        except Exception as e:
            logger.debug(f"[主动消息] 读取 metadata 版本失败喵: {e}")

        timer_cards = self._collect_timer_cards(now)

        return {
            "running": True,
            # 版本来源按优先级依次回退，保证控制台总能显示一个可读值。
            "version": getattr(self.plugin, "version", None)
            or getattr(self.plugin, "__version__", None)
            or metadata_version
            or "未知版本",
            "uptime_seconds": uptime_sec,
            # uptime 使用 datetime 差值字符串，便于直接面向人类展示。
            "uptime": str(
                datetime.fromtimestamp(now)
                - datetime.fromtimestamp(self.plugin.plugin_start_time)
            ),
            "scheduler_running": bool(
                self.plugin.scheduler and self.plugin.scheduler.running
            ),
            "sessions_count": len(self.plugin.session_data),
            "auto_trigger_timers": len(self.plugin.auto_trigger_timers),
            "group_timers": len(self.plugin.group_timers),
            "jobs_count": len(self.plugin.scheduler.get_jobs())
            if self.plugin.scheduler
            else 0,
            "timer_cards_total": len(timer_cards["auto_trigger_cards"])
            + len(timer_cards["group_timer_cards"]),
            "auto_trigger_cards": timer_cards["auto_trigger_cards"],
            "group_timer_cards": timer_cards["group_timer_cards"],
            "ws_connections": len(self._ws_connections),
            "timestamp": datetime.now().isoformat(),
        }

    def _collect_jobs(self) -> list[dict[str, Any]]:
        if not self.plugin.scheduler:
            return []

        jobs = []
        for job in self.plugin.scheduler.get_jobs():
            session_id = str(job.id)
            session_data = self.plugin.session_data.get(session_id, {})
            session_config = self.plugin._get_session_config(session_id) or {}
            jobs.append(
                {
                    "id": session_id,
                    "session_name": self.plugin._get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        session_id, session_config
                    ),
                    # APScheduler 的 next_run_time 是 datetime，这里统一序列化为 ISO 字符串。
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    # 以下字段用于前端推导进度条与调度窗口说明。
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                }
            )
        return jobs

    def _list_known_sessions(self) -> list[str]:
        sessions: set[str] = set()

        # 先收集全局配置里显式声明的会话。
        for scope_key in ("friend_settings", "group_settings"):
            cfg = self.config.get(scope_key, {})
            for session in cfg.get("session_list", []):
                if isinstance(session, str) and session:
                    sessions.add(self.plugin._normalize_session_id(session))

        # 再并入运行时数据与会话覆写记录，保证“曾经出现过”的会话也能在管理端看到。
        sessions.update(self.plugin.session_data.keys())
        sessions.update(self.plugin.session_override_manager.list_sessions())
        return sorted(sessions)

    def _list_known_session_summaries(self) -> list[dict[str, Any]]:
        """返回带展示信息的已知会话摘要（供 WS 实时推送使用）。"""
        result: list[dict[str, Any]] = []
        for session in self._list_known_sessions():
            effective = self.plugin._get_session_config(session)
            result.append(
                {
                    "session": session,
                    "session_name": self.plugin._get_session_name(session, effective),
                    "session_display_name": self.plugin._get_session_display_name(
                        session, effective
                    ),
                    "has_override": bool(
                        self.plugin.session_override_manager.get_override(session)
                    ),
                    "unanswered_count": self.plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                }
            )
        return result

    async def _broadcast_update(self, reason: str) -> None:
        if not self._ws_connections:
            return

        payload = {
            "type": "update",
            "reason": reason,
            "data": {
                "status": self._build_status_payload(),
                "jobs": self._collect_jobs(),
                "sessions": self._list_known_session_summaries(),
            },
        }

        to_remove: list[WebSocket] = []
        for ws in list(self._ws_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                # 某些连接可能已失活，先记录下来，循环结束后统一清理。
                to_remove.append(ws)

        for ws in to_remove:
            if ws in self._ws_connections:
                self._ws_connections.remove(ws)

    async def start(self) -> None:
        if not FASTAPI_AVAILABLE:
            logger.error("[主动消息] 无法启动 Web 管理端: FastAPI 未安装")
            return

        web_admin = self.config.get("web_admin", {})
        if not web_admin.get("enabled", False):
            logger.info("[主动消息] Web 管理端未启用喵。")
            return

        host = web_admin.get("host", "127.0.0.1")
        port = int(web_admin.get("port", 4100))

        # 采用 Uvicorn 内嵌启动，便于作为插件内部协程任务运行。
        uv_cfg = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uv_cfg)

        async def _serve():
            try:
                await self.server.serve()
            except Exception as e:
                logger.error(f"[主动消息] Web 管理端运行异常喵: {e}")

        self.server_task = asyncio.create_task(_serve())

        async def _cleanup_tokens_loop():
            while True:
                try:
                    await asyncio.sleep(3600)  # 每小时清理一次
                    now = time.time()
                    expired = [k for k, v in self._tokens.items() if now > v]
                    for k in expired:
                        self._tokens.pop(k, None)
                    if expired:
                        logger.debug(f"[主动消息] 已清理 {len(expired)} 个过期令牌。")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[主动消息] 清理过期令牌异常: {e}")

        if self._auth_enabled:
            self._token_cleanup_task = asyncio.create_task(_cleanup_tokens_loop())

        # 略等一个事件循环切片，让服务有机会完成绑定后再打印启动日志。
        await asyncio.sleep(0.1)
        logger.info(f"[主动消息] Web 管理端已启动喵: http://{host}:{port}")

    async def stop(self) -> None:
        if self._token_cleanup_task:
            self._token_cleanup_task.cancel()
        if self.server:
            # 通知 Uvicorn 进入优雅退出流程。
            self.server.should_exit = True

        if self.server_task:
            try:
                # 最多等待 5 秒，避免插件卸载时无限阻塞。
                await asyncio.wait_for(self.server_task, timeout=5)
            except Exception:
                pass

        self._ws_connections.clear()
        logger.info("[主动消息] Web 管理端已停止喵。")
