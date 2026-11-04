"""账号查询与登录校验。密码明文与哈希均不写日志。"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import verify_password
from app.core.logging import get_logger
from app.models import Account
from app.models.enums import AccountStatus

logger = get_logger(__name__)


class InvalidCredentials(Exception):
    """用户名或密码错误 / 账号不可用；路由层统一映射 401，不区分具体原因。"""


async def authenticate(session: AsyncSession, username: str, password: str) -> Account:
    """按用户名或手机号查找账号并校验密码；失败一律抛 InvalidCredentials。"""
    account = (
        await session.execute(
            select(Account).where(
                or_(Account.username == username, Account.phone == username)
            )
        )
    ).scalar_one_or_none()

    if (
        account is None
        or account.status != AccountStatus.ACTIVE
        or not verify_password(password, account.password_hash)
    ):
        logger.warning(
            "login failed",
            extra={"event": "login_failed"},
        )
        raise InvalidCredentials("invalid credentials")

    logger.info(
        "login succeeded",
        extra={
            "event": "login_succeeded",
            "actor_id": str(account.account_id),
            "actor_role": account.role,
        },
    )
    return account
