"""pytest 公共 fixture：应用与 ASGI Client。不要求连接数据库。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app as fastapi_app


@pytest.fixture
def app():
    return fastapi_app


@pytest.fixture
async def client(app) -> AsyncClient:
    """ASGI 内存客户端：不监听端口、不访问数据库。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
