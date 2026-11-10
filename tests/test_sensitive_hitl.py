"""Day6 P1-8 HITL 敏感事件 API 测试：admin 列表/流转、非 admin 403、审计字段。

workorders 复用口径（用户裁决）：type=sensitive_alert，status open→resolved
表达 pending→已处理；工单由 cascade safety 层产生（此处直插库造数）。
Day6+ 批6：resolve 开放 admin/nurse/doctor（abuse 仍仅 admin）、
原因说明+解决方案必填、按账号独立已读（workorder_reads）。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WorkorderPriority, WorkorderStatus
from app.models.workorder import Workorder

SENSITIVE_URL = "/api/v1/admin/sensitive-events"

RESOLVE_BODY = {"note": "已电话联系家属确认", "solution": "安排明日随访并放缓起身动作"}


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
    """Day6+：非 abuse 类目全角色可见（nurse 200）；批6 后 nurse/doctor 可 resolve。"""
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

    # family 无 resolve 权限（批6 后仍只读；body 传合法载荷验证 403 而非 422）
    resolve = await api.post(
        f"{SENSITIVE_URL}/{rows[0]['workorder_id']}/resolve",
        json=RESOLVE_BODY,
        headers=headers,
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
        json=RESOLVE_BODY,
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert body["solution"] == RESOLVE_BODY["solution"]
    assert body["read_by_me"] is True  # 批6：处理即视为已读
    assert "[原因说明] 已电话联系家属确认" in body["description"]

    # 落库审计复核
    await db_session.refresh(row)
    assert row.assignee_ref is not None and "admin" in row.assignee_ref
    assert row.solution == RESOLVE_BODY["solution"]


async def test_nurse_and_doctor_can_resolve_non_abuse(
    api, seed, db_session, auth_headers_factory
):
    """批6：resolve 开放 nurse/doctor（abuse 之外）；解决方案入独立列。"""
    for username in ("nurse_a", "doctor"):
        row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
        headers = await auth_headers_factory(username)
        resp = await api.post(
            f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
        )
        assert resp.status_code == 200, f"{username} 应可处理非 abuse 工单"
        assert resp.json()["status"] == "resolved"
        assert resp.json()["solution"] == RESOLVE_BODY["solution"]
        await db_session.refresh(row)
        assert row.solution == RESOLVE_BODY["solution"]


async def test_nurse_cannot_resolve_abuse(api, seed, db_session, auth_headers_factory):
    """批6：abuse 类目 nurse/doctor 不可见也不可流转（403）。"""
    row = await _make_alert(
        db_session, seed["elders"]["single"].elder_id, title="abuse 工单", category="abuse"
    )
    headers = await auth_headers_factory("nurse_a")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert resp.status_code == 403
    await db_session.refresh(row)
    assert row.status == WorkorderStatus.OPEN


async def test_resolve_requires_note_and_solution(api, seed, db_session, auth_headers_factory):
    """批6：原因说明/解决方案必填（缺任一 422）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")
    for bad in ({}, {"note": "只有说明"}, {"solution": "只有方案"}):
        resp = await api.post(
            f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=bad, headers=headers
        )
        assert resp.status_code == 422


async def test_read_endpoint_idempotent_and_per_account(
    api, seed, db_session, auth_headers_factory
):
    """批6：已读按账号独立、幂等；nurse 已读不影响 doctor 的未读状态。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    doctor = await auth_headers_factory("doctor")

    # 未读 → nurse 标已读 → 幂等重复 → doctor 仍未读
    first = await api.post(f"{SENSITIVE_URL}/{row.workorder_id}/read", headers=nurse)
    assert first.status_code == 200 and first.json() == {"read_by_me": True}
    again = await api.post(f"{SENSITIVE_URL}/{row.workorder_id}/read", headers=nurse)
    assert again.status_code == 200

    nurse_rows = await api.get(SENSITIVE_URL, headers=nurse)
    assert nurse_rows.json()[0]["read_by_me"] is True
    doctor_rows = await api.get(SENSITIVE_URL, headers=doctor)
    assert doctor_rows.json()[0]["read_by_me"] is False

    # nurse 对 abuse 不可见 → 标已读 403
    abuse = await _make_alert(
        db_session, seed["elders"]["single"].elder_id, title="abuse 工单", category="abuse"
    )
    resp = await api.post(f"{SENSITIVE_URL}/{abuse.workorder_id}/read", headers=nurse)
    assert resp.status_code == 403

    # 未知工单 404
    resp = await api.post(
        f"{SENSITIVE_URL}/00000000-0000-0000-0000-000000000000/read", headers=nurse
    )
    assert resp.status_code == 404


async def test_resolve_rejects_second_time_with_409(api, seed, db_session, auth_headers_factory):
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("admin")

    first = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert first.status_code == 200
    second = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=headers
    )
    assert second.status_code == 409


async def test_resolve_unknown_id_404(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    resp = await api.post(
        f"{SENSITIVE_URL}/00000000-0000-0000-0000-000000000000/resolve",
        json=RESOLVE_BODY,
        headers=headers,
    )
    assert resp.status_code == 404


async def test_unauthenticated_401(api, seed, db_session):
    await _make_alert(db_session, seed["elders"]["single"].elder_id)
    resp = await api.get(SENSITIVE_URL)
    assert resp.status_code == 401


# ---------------- 批7：处理记录 / 共同处理 / 协助请求 ----------------


async def test_resolve_writes_first_action_and_assist_flag(
    api, seed, db_session, auth_headers_factory
):
    """批7：resolve 写首条处理记录（角色/姓名快照+assist）；医生侧 assist_pending_for_me。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    doctor = await auth_headers_factory("doctor")

    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve",
        json={**RESOLVE_BODY, "assist_requested": True},
        headers=nurse,
    )
    assert resp.status_code == 200
    actions = resp.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["role"] == "nurse"
    assert actions[0]["actor_name"]
    assert actions[0]["assist_requested"] is True
    assert actions[0]["solution"] == RESOLVE_BODY["solution"]

    # 医生列表（status=all，resolved 工单默认列表不返回）：待我协助；护士自己与 admin 均不待协助
    doc_rows = (await api.get(SENSITIVE_URL + "?status=all", headers=doctor)).json()
    mine = next(r for r in doc_rows if r["workorder_id"] == str(row.workorder_id))
    assert mine["assist_pending_for_me"] is True
    nurse_rows = (await api.get(SENSITIVE_URL + "?status=all", headers=nurse)).json()
    mine2 = next(r for r in nurse_rows if r["workorder_id"] == str(row.workorder_id))
    assert mine2["assist_pending_for_me"] is False


async def test_supplement_appends_second_action_and_collab(
    api, seed, db_session, auth_headers_factory
):
    """批7：resolved 后医生补充 → 追加第二条记录不覆盖；主表 solution 更新为最新。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    nurse = await auth_headers_factory("nurse_a")
    doctor = await auth_headers_factory("doctor")

    first = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve",
        json={**RESOLVE_BODY, "assist_requested": True},
        headers=nurse,
    )
    assert first.status_code == 200

    sup = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/supplement",
        json={"solution": "复查血压正常，维持随访计划", "note": "基于今日随访结果"},
        headers=doctor,
    )
    assert sup.status_code == 200
    body = sup.json()
    assert len(body["actions"]) == 2
    assert {a["role"] for a in body["actions"]} == {"nurse", "doctor"}
    assert body["actions"][1]["role"] == "doctor"
    assert body["actions"][0]["solution"] == RESOLVE_BODY["solution"]  # 首条痕迹未被覆盖
    assert body["solution"] == "复查血压正常，维持随访计划"  # 主表保留最新
    assert "[补充说明] 基于今日随访结果" in body["description"]

    # 医生补充后待协助消除（status=all：resolved 工单不在默认 open 列表）
    doc_rows = (await api.get(SENSITIVE_URL + "?status=all", headers=doctor)).json()
    mine = next(r for r in doc_rows if r["workorder_id"] == str(row.workorder_id))
    assert mine["assist_pending_for_me"] is False

    # 再次补充允许（多次追加）
    again = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/supplement",
        json={"solution": "第三次随访确认无恙"},
        headers=nurse,
    )
    assert again.status_code == 200
    assert len(again.json()["actions"]) == 3


async def test_supplement_requires_resolved_409(api, seed, db_session, auth_headers_factory):
    """批7：open 状态不能直接补充（应先 resolve）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    headers = await auth_headers_factory("doctor")
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/supplement",
        json={"solution": "尚未处理就想补充"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_family_cannot_resolve_or_supplement(api, seed, db_session, auth_headers_factory):
    """批7：家属始终只读（resolve 与 supplement 均 403）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    family = await auth_headers_factory("family")
    assert (
        await api.post(
            f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=family
        )
    ).status_code == 403
    assert (
        await api.post(
            f"{SENSITIVE_URL}/{row.workorder_id}/supplement",
            json={"solution": "x"},
            headers=family,
        )
    ).status_code == 403


async def test_supplement_solution_required_422(api, seed, db_session, auth_headers_factory):
    """批7：补充的解决方案必填（缺省 422）。"""
    row = await _make_alert(db_session, seed["elders"]["single"].elder_id)
    admin = await auth_headers_factory("admin")
    await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/resolve", json=RESOLVE_BODY, headers=admin
    )
    resp = await api.post(
        f"{SENSITIVE_URL}/{row.workorder_id}/supplement", json={}, headers=admin
    )
    assert resp.status_code == 422
