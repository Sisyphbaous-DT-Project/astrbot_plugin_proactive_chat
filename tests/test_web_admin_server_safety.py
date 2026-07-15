from __future__ import annotations

import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

try:
    from astrbot.api.star import Context as _AstrBotContext  # noqa: F401
except Exception:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_star_module = types.ModuleType("astrbot.api.star")
    astrbot_star_module.Context = object

    class _FakeStarTools:
        @staticmethod
        def get_data_dir(_plugin_name: str) -> Path:
            return PLUGIN_ROOT / ".pytest_plugin_data"

    astrbot_star_module.StarTools = _FakeStarTools
    astrbot_api_module.logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    astrbot_api_module.star = astrbot_star_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.star", astrbot_star_module)

from astrbot_plugin_proactive_chat.core.plugin_lifecycle import LifecycleMixin  # noqa: E402
from astrbot_plugin_proactive_chat.core import web_admin_server  # noqa: E402


def _make_plugin(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        config={"web_admin": {"enabled": True, "host": "127.0.0.1", "port": port}},
    )


@pytest.mark.asyncio
async def test_web_admin_start_skips_when_port_is_in_use() -> None:
    if not web_admin_server.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        server = web_admin_server.WebAdminServer(_make_plugin(port))
        await server.start()

        assert server.server is None
        assert server.server_task is None
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_web_admin_start_isolates_uvicorn_system_exit(monkeypatch) -> None:
    if not web_admin_server.FASTAPI_AVAILABLE:
        pytest.skip("FastAPI is not installed")

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        async def serve(self) -> None:
            raise SystemExit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()

    monkeypatch.setattr(web_admin_server.uvicorn, "Server", FakeServer)

    server = web_admin_server.WebAdminServer(_make_plugin(port))
    await server.start()

    assert server.server is None
    assert server.server_task is None


@pytest.mark.asyncio
async def test_terminate_stops_web_admin_without_optional_components() -> None:
    class FakeWebAdminServer:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeLifecycle(LifecycleMixin):
        def __init__(self) -> None:
            self._background_tasks = set()
            self.group_timers = {}
            self.auto_trigger_timers = {}
            self.scheduler = None
            self.data_lock = None
            self.web_admin_server = FakeWebAdminServer()
            self.notification_center = None

        async def _flush_runtime_context_cache_save(self) -> None:
            return None

        async def _cleanup_background_tasks(self) -> None:
            return None

    plugin = FakeLifecycle()

    await plugin.terminate()

    assert plugin.web_admin_server.stopped is True
