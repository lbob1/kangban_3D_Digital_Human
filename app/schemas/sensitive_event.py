"""敏感事件 HITL 契约（Day6 P1-8）：workorders(type=sensitive_alert) 的视图。

批7（2026-11-10）：处理记录化——resolve 写首条 workorder_actions，
resolved 后另一角色可补充（追加不覆盖）；响应带 actions 时间线、
共同处理徽标依据（actions 角色≥2）与「待我协助」标记。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SensitiveEventAction(BaseModel):
    """单条处理记录（含首条 resolve 与后续补充；审核留痕见 action 字段）。"""

    model_config = ConfigDict(from_attributes=True)

    role: str
    actor_name: str
    note: str | None = None
    solution: str
    # 动作类型（0015）：resolve/supplement/review_approve/review_reject；历史行 None
    action: str | None = None
    assist_requested: bool = False
    created_at: datetime


class SensitiveEventResponse(BaseModel):
    """敏感事件（复用护工工单表；status open→resolved 表达 pending→已处理）。"""

    model_config = ConfigDict(from_attributes=True)

    workorder_id: UUID
    elder_id: UUID
    status: str
    priority: str
    # 类目（0009 迁移）：acute_symptom/fall/lost/emotional_crisis/emergency_call/abuse；
    # abuse 仅 admin+family 可见（Day6+ 可见性矩阵）
    category: str = "general"
    title: str
    description: str | None = None
    # 解决方案（0010 迁移）：最新一条处理记录的方案（家属端列表简要展示；
    # 完整时间线见 actions）
    solution: str | None = None
    # 批6：当前请求账号是否已读（按账号独立，workorder_reads 表推导）
    read_by_me: bool = False
    # 批7：处理记录时间线（resolve 首条 + 历次补充，按时间升序）
    actions: list[SensitiveEventAction] = []
    # 批7：是否待当前请求角色协助（对方 resolve 时勾选了「请我协助」且我方尚无记录）
    assist_pending_for_me: bool = False
    # 审核终态制（0015 迁移，D3）：pending/approved/rejected；open 工单与历史行为 None
    review_status: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class SensitiveEventResolve(BaseModel):
    """标记已处理（批6 开放 admin/nurse/doctor）：原因说明与解决方案均必填。

    批7：assist_requested=勾选「请医生/护士协助」，工单 resolved 后由另一角色补充，
    详情显示「护工+医生 共同处理」。
    """

    note: str = Field(min_length=1, description="原因说明（追加到 description，审计留痕）")
    solution: str = Field(min_length=1, description="解决方案（独立列，家属端可见）")
    assist_requested: bool = Field(default=False, description="是否请另一角色（医生/护士）协助处理")


class SensitiveEventSupplement(BaseModel):
    """批7：resolved 后补充/完善解决方案（追加处理记录，不覆盖既有痕迹）。"""

    solution: str = Field(min_length=1, description="补充/完善的解决方案")
    note: str | None = Field(default=None, description="补充说明（可选）")


class SensitiveEventReview(BaseModel):
    """审核终态制（D3）：admin 对 resolved+pending 工单做终审。

    action=approve → 终态「已审核」；action=reject → 工单回 open 重处理。
    review_note 必填（驳回必须说明理由，approve 也留痕）。
    """

    action: str = Field(pattern="^(approve|reject)$", description="审核动作")
    review_note: str = Field(min_length=1, description="审核意见（必填，留痕）")
