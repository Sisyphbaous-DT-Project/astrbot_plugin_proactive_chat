from __future__ import annotations

import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = SimpleNamespace(
    debug=lambda *_args, **_kwargs: None,
    info=lambda *_args, **_kwargs: None,
    warning=lambda *_args, **_kwargs: None,
    error=lambda *_args, **_kwargs: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

from astrbot_plugin_proactive_chat.core import web_admin_server


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
