"""Day6 P1-8 HITL 敏感事件 API 测试：admin 列表/流转、非 admin 403、审计字段。

workorders 复用口径（用户裁决）：type=sensitive_alert，status open→resolved
表达 pending→已处理；工单由 cascade safety 层产生（此处直插库造数）。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WorkorderPriority, WorkorderStatus
from app.models.workorder import Workorder

SENSITIVE_URL = "/api/v1/admin/sensitive-events"


async def _make_alert(
    db_session: AsyncSession,
    elder_id,
    title="敏感事件：急性症状",
    category: str = "acute_symptom",
) -> Workorder:
    row = Workorder(
        elder_id=elder_id,
        type="sensitive_alert",
        category=category,
        status=WorkorderStatus.OPEN,
        priority=WorkorderPriority.URGENT,
        title=title,
        description="我胸口疼（脱敏后）",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_sensitive_visible_to_nurse_non_abuse(api, seed, db_session, auth_headers_factory):
    """Day6+：非 abuse 类目全角色可见（nurse 200），resolve 仍仅 admin。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("nurse_a")
    resp = await api.get(SENSITIVE_URL, headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["workorder_id"] == str(row.workorder_id)


async def test_abuse_hidden_from_nurse_and_doctor(api, seed, db_session, auth_headers_factory):
    """Day6+ 虐待排除矩阵：abuse 工单 nurse/doctor 不可见（施害者可能是工作人员）。"""
    await _make_alert(
        db_session,
        seed["elders"]["single"].elder_id,
        title="敏感事件：abuse",
        category="abuse",
    )
    for username in ("nurse_a", "doctor"):
        resp = await api.get(SENSITIVE_URL, headers=await auth_headers_factory(username))
        assert resp.status_code == 200
        assert resp.json() == [], f"{username} 不应看到 abuse 工单"
    admin_rows = await api.get(SENSITIVE_URL, headers=await auth_headers_factory("admin"))
    assert len(admin_rows.json()) == 1  # admin 全见


async def test_family_reads_only_linked_elders(api, seed, db_session, auth_headers_factory):
    """family：只见自己关联长者的工单（含 abuse），无关长者不可见。"""
    from app.models import FamilyElderLink

    linked = seed["elders"]["single"]
    other = seed["elders"]["other"]
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=linked.elder_id,
            relation="son",
            status="active",
        )
    )
    await db_session.commit()
    await _make_alert(db_session, linked.elder_id, title="敏感事件：跌倒", category="fall")
    await _make_alert(
        db_session, other.elder_id, title="敏感事件：abuse-other", category="abuse"
    )

    headers = await auth_headers_factory("family")
    resp = await api.get(SENSITIVE_URL, headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1  # 只见关联长者（single）的工单
    assert rows[0]["elder_id"] == str(linked.elder_id)

    # family 无 resolve 权限
    resolve = await api.post(
        f"{SENSITIVE_URL}/{rows[0]['workorder_id']}/resolve", json={}, headers=headers
    )
    assert resolve.status_code == 403


async def test_sensitive_list_open_default_and_filter(
    api, seed, db_session, auth_headers_factory
):
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")

    resp = await api.get(SENSITIVE_URL, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["workorder_id"] == str(row.workorder_id)
    assert body[0]["status"] == "open"
    assert body[0]["title"].startswith("敏感事件")

    resolved = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    resolved.status = WorkorderStatus.RESOLVED
    await db_session.commit()

    only_open = await api.get(SENSITIVE_URL + "?status=open", headers=headers)
    assert len(only_open.json()) == 1
    all_rows = await api.get(SENSITIVE_URL + "?status=all", headers=headers)
    assert len(all_rows.json()) == 2


async def test_resolve_flow_and_audit_fields(api, seed, db_session, auth_headers_factory):
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")

    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve",
        json={"note": "已电话联系家属确认"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert "[处理备注] 已电话联系家属确认" in body["description"]

    # 落库审计复核
    await db_session.refresh(row)
    assert row.assignee_ref is not None and "admin" in row.assignee_ref


async def test_resolve_rejects_second_time_with_409(api, seed, db_session, auth_headers_factory):
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")

    first = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json={}, headers=headers
    )
    assert first.status_code == 200
    second = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json={}, headers=headers
    )
    assert second.status_code == 409


async def test_resolve_unknown_id_404(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    resp = await api.post(
        f"{SENSITIVE_URL}/00000000-0000-0000-0000-000000000000/resolve",
        json={},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_unauthenticated_401(api, seed, db_session):
    await _make_alert(db_session, seed["elders"]["single"].elder_id)
    resp = await api.get(SENSITIVE_URL)
    assert resp.status_code == 401
