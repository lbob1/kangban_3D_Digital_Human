"""审核终态制（D3，0015 迁移）API 测试：护工/医生 resolve→pending、
admin resolve→自审终态 approved、review approve/reject 流转、409/403/422 边界。

流转口径（用户 2026-11-10 拍板）：
- nurse/doctor resolve → status=resolved + review_status=pending；
- admin resolve → resolved + approved + reviewed_by=admin（自审免审）；
- review approve：仅 resolved+pending → approved 终态；
- review reject：→ rejected 且 status 回 open（solution 保留供重处理参考），
  时间线追加 review_approve/review_reject 留痕。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WorkorderStatus
from app.models.workorder import Workorder

SENSITIVE_URL = "/api/v1/admin/sensitive-events"

RESOLVE_BODY = {"note": "已现场查看老人", "solution": "联系家属并安排随访"}


async def _make_alert(db_session: AsyncSession, elder_id) -> Workorder:
    row = Workorder(
        elder_id=elder_id,
        type="sensitive_alert",
        category="acute_symptom",
        status=WorkorderStatus.OPEN,
        priority="urgent",
        title="敏感事件：审核流测试",
        description="我胸口疼（脱敏后）",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_nurse_resolve_sets_review_pending(api, seed, db_session, auth_headers_factory):
    """护工 resolve → resolved + review_status=pending（前端「已处理·待审核」）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("nurse_a")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["review_status"] == "pending"
    assert body["reviewed_by"] is None and body["reviewed_at"] is None
    await db_session.refresh(row)
    assert row.review_status == "pending"


async def test_doctor_resolve_sets_review_pending(api, seed, db_session, auth_headers_factory):
    """医生 resolve 同样进待审核队列。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("doctor")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "pending"


async def test_admin_resolve_directly_approved(api, seed, db_session, auth_headers_factory):
    """admin resolve 自审即终态：approved + reviewed_by 落管理员本人。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["reviewed_by"]
    assert body["reviewed_at"] is not None
    await db_session.refresh(row)
    assert row.review_status == "approved" and row.reviewed_by is not None


async def _resolve_by_nurse(api, row, headers):
    return await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )


async def test_review_approve_final_state(api, seed, db_session, auth_headers_factory):
    """admin approve：仅 resolved+pending → approved 终态，审核字段齐全。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    admin = await auth_headers_factory("admin")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200

    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/review",
        json={"action": "approve", "review_note": "处理方案合理，通过"},
        headers=admin,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["review_status"] == "approved"
    assert body["review_note"] == "处理方案合理，通过"
    assert body["reviewed_by"] and body["reviewed_at"]
    # 时间线留痕：resolve + review_approve
    actions = body["actions"]
    assert [a["action"] for a in actions] == ["resolve", "review_approve"]
    assert actions[1]["role"] == "admin"
    assert actions[1]["solution"] == "处理方案合理，通过"


async def test_review_reject_back_to_open(api, seed, db_session, auth_headers_factory):
    """admin reject：review_status=rejected 且 status 回 open；solution 保留；时间线留痕。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    admin = await auth_headers_factory("admin")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200

    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/review",
        json={"action": "reject", "review_note": "方案不完整，请补充复测记录"},
        headers=admin,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "rejected"
    assert body["status"] == "open"
    assert body["review_note"] == "方案不完整，请补充复测记录"
    assert body["solution"] == RESOLVE_BODY["solution"]  # 原 solution 保留供参考
    assert [a["action"] for a in body["actions"]] == ["resolve", "review_reject"]

    await db_session.refresh(row)
    assert row.status == WorkorderStatus.OPEN and row.review_status == "rejected"

    # 驳回后护工可重新 resolve → 再次进入待审核
    again = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve",
        json={**RESOLVE_BODY, "solution": "补充复测记录后重新提交"},
        headers=nurse,
    )
    assert again.status_code == 200
    assert again.json()["review_status"] == "pending"


async def test_review_duplicate_409(api, seed, db_session, auth_headers_factory):
    """重复审核 409：approve 终态后不可再审。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    admin = await auth_headers_factory("admin")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200
    first = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/review",
        json={"action": "approve", "review_note": "通过"},
        headers=admin,
    )
    assert first.status_code == 200
    second = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/review",
        json={"action": "approve", "review_note": "再审一次"},
        headers=admin,
    )
    assert second.status_code == 409


async def test_review_non_pending_state_409(api, seed, db_session, auth_headers_factory):
    """状态不符 409：open 工单不能直接审核。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    admin = await auth_headers_factory("admin")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/review",
        json={"action": "approve", "review_note": "尚未处理就想审核"},
        headers=admin,
    )
    assert resp.status_code == 409


async def test_review_requires_admin_403(api, seed, db_session, auth_headers_factory):
    """非 admin 调 review 403（护工/医生/家属均不可审）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200
    for username in ("nurse_a", "doctor", "family"):
        headers = await auth_headers_factory(username)
        resp = await api.post(
            f"{SENSITIVE_URL}/{row.workorder_id}/review",
            json={"action": "approve", "review_note": "越权尝试"},
            headers=headers,
        )
        assert resp.status_code == 403, f"{username} 不应可审核"


async def test_review_validation_422(api, seed, db_session, auth_headers_factory):
    """422 三例：缺 review_note、非法 action、空 review_note。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    admin = await auth_headers_factory("admin")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200
    url = f"{SENSITIVE_URL}/{row.workorder_id}/review"
    assert (
        await api.post(url, json={"action": "approve"}, headers=admin)
    ).status_code == 422  # 缺 review_note
    assert (
        await api.post(url, json={"review_note": "意见"}, headers=admin)
    ).status_code == 422  # 缺 action
    assert (
        await api.post(url, json={"action": "defer", "review_note": "意见"}, headers=admin)
    ).status_code == 422  # 非法 action


async def test_review_unknown_id_404(api, seed, auth_headers_factory):
    admin = await auth_headers_factory("admin")
    resp = await api.post(
        f"{SENSITIVE_URL}/00000000-0000-0000-0000-000000000000/review",
        json={"action": "approve", "review_note": "x"},
        headers=admin,
    )
    assert resp.status_code == 404


async def test_review_pending_filter_and_widget_states(
    api, seed, db_session, auth_headers_factory
):
    """待审核筛选（status=review_pending）+ 三端响应携带审核态字段（家属只读可见）。"""
    from app.models import FamilyElderLink

    linked = seed["elders"]["single"]
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=linked.elder_id,
            relation="son",
            status="active",
        )
    )
    await db_session.commit()
    row = await _make_alert(db_session, linked.elder_id)
    nurse = await auth_headers_factory("nurse_a")
    assert (await _resolve_by_nurse(api, row, nurse)).status_code == 200

    admin = await auth_headers_factory("admin")
    pending = await api.get(SENSITIVE_URL + "?status=review_pending", headers=admin)
    assert pending.status_code == 200
    assert [r["workorder_id"] for r in pending.json()] == [str(row.workorder_id)]

    # family 端响应同样带 review_status（前端三态标签数据源；家属无 resolve/review 入口）
    fam = await api.get(SENSITIVE_URL + "?status=all", headers=await auth_headers_factory("family"))
    assert fam.status_code == 200
    mine = next(r for r in fam.json() if r["workorder_id"] == str(row.workorder_id))
    assert mine["review_status"] == "pending"
