"""通知实时化 WS 端点与连接管理器（Day9 步 7，主文档 §4.4 / D2，收 Day8 遗留①）。

口径：
- `/ws/notify`：**用户 JWT 鉴权**（浏览器 WebSocket 无法自定义 Header——token 走
  查询参数；设备 Token 到此关闭 403 语义：人/设备互斥红线不变）；
- 连接管理器记录 (role, account_id)——**范围过滤只在服务端做**：推送时按事件
  elder_id 逐连接复用 workqueue 同款判定语义（authorization.can_access_elder：
  护工=本楼栋、医生/家属=有效关联、admin=全量）；客户端不做权限判断；
- 推送事件为结构化 dict（不含对话历史/病历明细）；连接断开静默清理。
"""

from __future__ import annotations

from uuid import UUID

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.auth.jwt import USER_TOKEN_TYPE, TokenTypeMismatch, decode_token
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models import Account
from app.models.enums import AccountStatus
from app.services.authorization import AccessDecision, can_access_elder

logger = get_logger("realtime.notify_ws")

router = APIRouter()


class NotifyConnectionManager:
    """在线管理端连接注册表（进程内；教学口径单进程足够）。"""

    def __init__(self) -> None:
        self._connections: dict[WebSocket, tuple[str, UUID]] = {}  # ws → (role, account_id)

    def connect(self, ws: WebSocket, role: str, account_id: UUID) -> None:
        self._connections[ws] = (role, account_id)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)

    @property
    def online(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: dict) -> int:
        """按服务端范围过滤推送：每连接独立判定（复用 can_access_elder）。

        elder_id 缺失（全局事件）→ 仅 admin 连接收；判定异常/发送失败的连接
        静默清理（离线兜底=落日志/落库，不丢通知）。
        """
        sent = 0
        elder_id = event.get("elder_id")
        session_factory = get_session_factory()
        for ws, (role, account_id) in list(self._connections.items()):
            try:
                allowed = False
                if elder_id is None:
                    allowed = role == "admin"
                elif role == "admin":
                    allowed = True
                else:
                    async with session_factory() as session:
                        user = type("U", (), {"account_id": account_id, "role": role})
                        from app.models import Elder

                        elder = (
                            await session.execute(
                                select(Elder).where(Elder.elder_id == UUID(str(elder_id)))
                            )
                        ).scalars().first()
                        allowed = elder is not None and (
                            await can_access_elder(session, user, elder)
                        ) == AccessDecision.ALLOWED
                if allowed:
                    await ws.send_json(event)
                    sent += 1
            except Exception:  # noqa: BLE001 —— 单连接失败不影响其他接收方
                self.disconnect(ws)
        if event.get("notify_kind"):
            logger.info(
                "notify ws broadcast",
                extra={
                    "event": "notify_ws_broadcast",
                    "notify_kind": event.get("notify_kind"),
                    "online": self.online,
                    "sent": sent,
                },
            )
        return sent


manager = NotifyConnectionManager()


async def _resolve_ws_user(ws: WebSocket, token: str):
    """WS 握手鉴权：人 JWT 验签 + 账号 active 校验（复用 get_current_user 同语义）。"""
    try:
        payload = decode_token(token, USER_TOKEN_TYPE)
    except (jwt.InvalidTokenError, TokenTypeMismatch):
        return None
    account_id = payload.get("sub")
    if not account_id:
        return None
    async with get_session_factory()() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == UUID(str(account_id)))
            )
        ).scalar_one_or_none()
    if account is None or account.status != AccountStatus.ACTIVE:
        return None
    return account


@router.websocket("/ws/notify")
async def notify_ws_endpoint(
    ws: WebSocket, token: str = Query(default="")
) -> None:
    """管理端通知 WS：用户 JWT 鉴权；范围过滤在服务端（客户端不做权限判断）。"""
    account = await _resolve_ws_user(ws, token)
    if account is None:
        await ws.close(code=4401)  # 鉴权失败：401 语义（WS 握手后关闭码）
        return
    await ws.accept()
    manager.connect(ws, role=account.role, account_id=account.account_id)
    logger.info(
        "notify ws connected",
        extra={"event": "notify_ws_connected", "role": account.role, "online": manager.online},
    )
    try:
        while True:
            # 客户端只收不发：收到任何消息（含 ping）忽略——权限判断永不信任客户端
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
        logger.info(
            "notify ws disconnected",
            extra={
                "event": "notify_ws_disconnected",
                "role": account.role,
                "online": manager.online,
            },
        )
