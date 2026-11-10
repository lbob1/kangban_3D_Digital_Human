"""媒体库检索与播放上报路由（Day7 步5，设备 token 鉴权）。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_device
from app.db.session import get_session
from app.schemas.media import MediaHistoryResponse, MediaItemResponse, MediaPlayReport
from app.services import media as media_service

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/library", response_model=list[MediaItemResponse])
async def list_library_route(
    device_id: UUID = Depends(get_current_device),
    media_type: str | None = Query(default=None, alias="type"),
    genre: str | None = Query(default=None),
    era: str | None = Query(default=None),
    limit: int = Query(default=100, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[MediaItemResponse]:
    """active + 授权版权媒体列表（版权 CHECK 兜底 + 服务层同口径过滤）。"""
    items = await media_service.list_library(
        session, media_type=media_type, genre=genre, era=era, limit=limit
    )
    return [MediaItemResponse.model_validate(m) for m in items]


@router.post(
    "/history", response_model=MediaHistoryResponse, status_code=status.HTTP_201_CREATED
)
async def report_play_route(
    body: MediaPlayReport,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> MediaHistoryResponse:
    """播放上报落库：elder_id 由服务端经设备绑定解析（不信任请求体，安全红线 8）。"""
    try:
        history = await media_service.record_play(session, device_id=device_id, data=body)
    except media_service.MediaNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except media_service.MediaInactive as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except media_service.DeviceNotBound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except media_service.ElderAmbiguous as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return MediaHistoryResponse.model_validate(history)
