"""Day8 步6：护工 workqueue 端点测试（四类聚合 + 楼栋隔离 + HITL 接图）。

走 kangban_test 真实库（事务回滚隔离）；health_alert 工单经登记端点 e2e 产生
（三通道共用一图），确认/升级走 Command(resume) 续图，回查 DB 断言流转结果。
"""

import pytest
from sqlalchemy import select

from app.models import BuildingAssignment, DoctorElderAssignment, Workorder
from app.models.enums import AssignmentStatus, WorkorderPriority


@pytest.fixture(autouse=True)
def _ck_path(monkeypatch, tmp_path):
    """接图 e2e：checkpoint 指向临时文件，不污染 data/。"""
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "health_checkpoint_db", str(tmp_path / "ck.sqlite")
    )


async def _grant_nurse_building(db_session, seed, building_code: str = "A") -> None:
    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id,
            building_code=building_code,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def _grant_doctor_elder(db_session, seed, elder) -> None:
    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=elder.elder_id,
            status=AssignmentStatus.ACTIVE,
        )
    )
    await db_session.flush()


async def _register(api, headers, elder, **overrides) -> dict:
    payload = {"bp_systolic": 130, "bp_diastolic": 85}
    payload.update(overrides)
    resp = await api.post(
        f"/api/v1/health-records?elder_id={elder.elder_id}",
        headers=headers,
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_yellow(api, db_session, seed, headers, elder) -> Workorder:
    """登记 yellow（情绪 ≤2）→ health_alert/high 工单挂护工确认。"""
    await _register(api, headers, elder, mood=2)
    return (
        await db_session.execute(
            select(Workorder)
            .where(Workorder.type == "health_alert")
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


async def _make_red(api, db_session, seed, headers, elder) -> Workorder:
    """主诉急症词 → red/urgent 工单挂医生确认。"""
    await _register(api, headers, elder, chief_complaint="胸口疼")
    return (
        await db_session.execute(
            select(Workorder)
            .where(Workorder.type == "health_alert")
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


def _plain_workorder(elder, *, type: str, category: str = "general") -> Workorder:
    return Workorder(
        elder_id=elder.elder_id,
        type=type,
        category=category,
        priority=WorkorderPriority.HIGH,
        title=f"测试工单 {type}",
        description="步6 单测插入",
    )


# ---- GET /nurse/workqueue：聚合 + 楼栋隔离（硬验收）----


async def test_workqueue_building_isolation(api, seed, auth_headers_factory, db_session):
    """nurse 只见本楼栋长者工单；admin 全量（范围外 404 隐藏存在性由 resolve 用例覆盖）。"""
    nurse_h = await auth_headers_factory("nurse_a")
    admin_h = await auth_headers_factory("admin")
    await _grant_nurse_building(db_session, seed)
    await _make_yellow(api, db_session, seed, nurse_h, seed["elders"]["single"])  # A 楼
    await _make_yellow(api, db_session, seed, admin_h, seed["elders"]["other"])  # B 楼

    resp = await api.get("/api/v1/nurse/workqueue", headers=nurse_h)
    assert resp.status_code == 200, resp.text
    names = {it["elder_name"] for it in resp.json()}
    assert names == {"王单间"}  # B 楼「钱他楼」不可见

    resp = await api.get("/api/v1/nurse/workqueue", headers=admin_h)
    assert resp.status_code == 200
    names = {it["elder_name"] for it in resp.json()}
    assert names == {"王单间", "钱他楼"}


async def test_workqueue_four_types_and_abuse_excluded(api, seed, auth_headers_factory, db_session):
    """四类聚合（含非 abuse 敏感事件）；nurse 排除 abuse，admin 可见。"""
    nurse_h = await auth_headers_factory("nurse_a")
    admin_h = await auth_headers_factory("admin")
    await _grant_nurse_building(db_session, seed)
    elder = seed["elders"]["single"]
    await _make_yellow(api, db_session, seed, nurse_h, elder)
    db_session.add_all(
        [
            _plain_workorder(elder, type="medication_missed"),
            _plain_workorder(elder, type="help_request"),
            _plain_workorder(elder, type="sensitive_alert", category="fall"),
            _plain_workorder(elder, type="sensitive_alert", category="abuse"),
        ]
    )
    await db_session.flush()

    resp = await api.get("/api/v1/nurse/workqueue", headers=nurse_h)
    assert resp.status_code == 200
    items = resp.json()
    types = sorted(it["type"] for it in items)
    assert types == ["health_alert", "help_request", "medication_missed", "sensitive_alert"]
    # nurse 排除 abuse；health_alert 附图挂起站点
    wo = next(it for it in items if it["type"] == "health_alert")
    assert wo["pending_site"] == "nurse_confirm"
    assert wo["status"] == "open" and wo["priority"] == "high"

    resp = await api.get("/api/v1/nurse/workqueue", headers=admin_h)
    items = resp.json()
    assert len(items) == 5  # admin 可见 abuse
    assert any(it["category"] == "abuse" for it in items)


# ---- POST resolve：yellow 续图 + 幂等 409 + 越权 404 ----


async def test_resolve_yellow_acknowledged_and_idempotent_409(
    api, seed, auth_headers_factory, db_session
):
    """护工确认 yellow → 图续跑工单流转 acknowledged（非 0015 链的 resolved）；重复 409。"""
    nurse_h = await auth_headers_factory("nurse_a")
    await _grant_nurse_building(db_session, seed)
    wo = await _make_yellow(api, db_session, seed, nurse_h, seed["elders"]["single"])

    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "电话复核无异常", "solution": "已联系家属到场陪护"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "acknowledged"
    assert body["pending_site"] is None

    # 幂等：已流转 409
    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "x", "solution": "y"},
    )
    assert resp.status_code == 409


async def test_resolve_out_of_scope_404(api, seed, auth_headers_factory, db_session):
    """B 楼长者工单对 A 楼护工 404（隐藏存在性，不泄露工单存在）。"""
    admin_h = await auth_headers_factory("admin")
    nurse_h = await auth_headers_factory("nurse_a")
    await _grant_nurse_building(db_session, seed)
    wo = await _make_yellow(api, db_session, seed, admin_h, seed["elders"]["other"])

    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "x", "solution": "y"},
    )
    assert resp.status_code == 404


async def test_resolve_non_health_alert_0015_chain(api, seed, auth_headers_factory, db_session):
    """非 health_alert（help_request）→ 0015 审核链：resolved + pending + 留痕；重复 409。"""
    nurse_h = await auth_headers_factory("nurse_a")
    await _grant_nurse_building(db_session, seed)
    elder = seed["elders"]["single"]
    db_session.add(_plain_workorder(elder, type="help_request"))
    await db_session.flush()
    wo = (
        await db_session.execute(select(Workorder).where(Workorder.type == "help_request"))
    ).scalar_one()

    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "已上门处理", "solution": "更换了跌倒报警器电池"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    row = await db_session.get(Workorder, wo.workorder_id)
    assert row.review_status == "pending"  # nurse resolve → 待审核
    assert row.solution == "更换了跌倒报警器电池"
    assert row.assignee_ref and "nurse_a" in row.assignee_ref

    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "x", "solution": "y"},
    )
    assert resp.status_code == 409


# ---- POST escalate + 医生确认：yellow 升级 / red 直达闭环 ----


async def test_escalate_yellow_then_doctor_confirm(
    api, seed, auth_headers_factory, db_session
):
    """护工升级 yellow → 工单升 urgent 进医生 interrupt；医生确认闭环（通知+转诊）。"""
    nurse_h = await auth_headers_factory("nurse_a")
    doctor_h = await auth_headers_factory("doctor")
    await _grant_nurse_building(db_session, seed)
    elder = seed["elders"]["single"]
    await _grant_doctor_elder(db_session, seed, elder)
    wo = await _make_yellow(api, db_session, seed, nurse_h, elder)

    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/escalate",
        headers=nurse_h,
        json={"note": "血压复测仍偏高"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "open"
    assert body["priority"] == "urgent"
    assert body["pending_site"] == "doctor_confirm"

    # 升级后护工 resolve → 409（待医生确认）
    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/resolve",
        headers=nurse_h,
        json={"note": "x", "solution": "y"},
    )
    assert resp.status_code == 409
    # 重复升级 → 409（已在医生环节）
    resp = await api.post(
        f"/api/v1/nurse/workqueue/{wo.workorder_id}/escalate",
        headers=nurse_h,
        json={},
    )
    assert resp.status_code == 409

    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm",
        headers=doctor_h,
        json={"note": "同意升级转诊"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "acknowledged"
    assert body["pending_site"] is None
    row = await db_session.get(Workorder, wo.workorder_id)
    assert "转诊单号" in (row.description or "")


async def test_doctor_confirm_red_direct_and_idempotent(
    api, seed, auth_headers_factory, db_session
):
    """red 工单医生直达确认 → acknowledged + 转诊留痕；重复确认 409。"""
    doctor_h = await auth_headers_factory("doctor")
    elder = seed["elders"]["single"]
    await _grant_doctor_elder(db_session, seed, elder)
    wo = await _make_red(
        api, db_session, seed, await auth_headers_factory("admin"), elder
    )
    assert wo.priority == WorkorderPriority.URGENT

    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm",
        headers=doctor_h,
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "acknowledged"

    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm", headers=doctor_h, json={}
    )
    assert resp.status_code == 409


async def test_doctor_out_of_scope_404_and_yellow_confirm(
    api, seed, auth_headers_factory, db_session
):
    """医生范围外 404；yellow 挂护工站点时医生也可直接确认（归档不通知，无转诊单号）。"""
    doctor_h = await auth_headers_factory("doctor")
    nurse_h = await auth_headers_factory("nurse_a")
    await _grant_nurse_building(db_session, seed)
    wo = await _make_yellow(
        api, db_session, seed, nurse_h, seed["elders"]["single"]
    )  # doctor 无关联

    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm", headers=doctor_h, json={}
    )
    assert resp.status_code == 404  # 范围外隐藏存在性

    # 授予关联后：yellow 仍挂护工站点，医生确认 → 走护工同路径归档（不发通知）
    await _grant_doctor_elder(db_session, seed, seed["elders"]["single"])
    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm", headers=doctor_h, json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "acknowledged"
    assert body["pending_site"] is None
    row = await db_session.get(Workorder, wo.workorder_id)
    assert "转诊单号" not in (row.description or "")  # yellow 路径零副作用（对比 red）


async def test_workqueue_nurse_family_403(api, seed, auth_headers_factory):
    """角色门禁：family/doctor 不可访问 workqueue。"""
    fam = await auth_headers_factory("family")
    resp = await api.get("/api/v1/nurse/workqueue", headers=fam)
    assert resp.status_code == 403
    doc = await auth_headers_factory("doctor")
    resp = await api.get("/api/v1/nurse/workqueue", headers=doc)
    assert resp.status_code == 403


# ---- GET /doctor/health-alerts：医生端健康急警待办列表（Day12 UI 闭环）----


async def test_doctor_health_alerts_list_scope_and_sites(
    api, seed, auth_headers_factory, db_session
):
    """doctor 只见关联长者的 health_alert；yellow 显护工站点、red 显医生站点；admin 全量。"""
    doctor_h = await auth_headers_factory("doctor")
    admin_h = await auth_headers_factory("admin")
    nurse_h = await auth_headers_factory("nurse_a")
    await _grant_nurse_building(db_session, seed)
    await _grant_doctor_elder(db_session, seed, seed["elders"]["single"])

    yellow = await _make_yellow(api, db_session, seed, nurse_h, seed["elders"]["single"])
    await _make_red(api, db_session, seed, admin_h, seed["elders"]["single"])
    # 范围外长者的单（doctor 无关联，列表必须不可见）
    await _make_red(api, db_session, seed, admin_h, seed["elders"]["other"])

    # 连续登记时间戳可能并列（created_at 精度），按优先级稳健定位本长者的两单
    rows = (
        await db_session.execute(
            select(Workorder).where(
                Workorder.elder_id == seed["elders"]["single"].elder_id,
                Workorder.type == "health_alert",
            )
        )
    ).scalars().all()
    red = next(w for w in rows if w.priority == WorkorderPriority.URGENT)
    assert yellow.workorder_id in {w.workorder_id for w in rows}

    resp = await api.get("/api/v1/doctor/health-alerts", headers=doctor_h)
    assert resp.status_code == 200, resp.text
    items = resp.json()
    ids = {it["workorder_id"] for it in items}
    assert str(yellow.workorder_id) in ids and str(red.workorder_id) in ids
    assert all(it["elder_name"] == "王单间" for it in items)  # 范围外「钱他楼」不可见
    sites = {it["workorder_id"]: it["pending_site"] for it in items}
    assert sites[str(red.workorder_id)] == "doctor_confirm"
    assert sites[str(yellow.workorder_id)] == "nurse_confirm"

    # admin 全量（含范围外长者）
    resp = await api.get("/api/v1/doctor/health-alerts", headers=admin_h)
    assert resp.status_code == 200
    assert len(resp.json()) == 3

    # 角色门禁：family 403
    fam = await auth_headers_factory("family")
    resp = await api.get("/api/v1/doctor/health-alerts", headers=fam)
    assert resp.status_code == 403


async def test_doctor_health_alerts_status_filter_after_confirm(
    api, seed, auth_headers_factory, db_session
):
    """确认后 status=open 不再包含；status=all 仍可见（acknowledged，pending_site 清空）。"""
    doctor_h = await auth_headers_factory("doctor")
    elder = seed["elders"]["single"]
    await _grant_doctor_elder(db_session, seed, elder)
    wo = await _make_red(
        api, db_session, seed, await auth_headers_factory("admin"), elder
    )

    resp = await api.get("/api/v1/doctor/health-alerts", headers=doctor_h)
    assert any(it["workorder_id"] == str(wo.workorder_id) for it in resp.json())

    resp = await api.post(
        f"/api/v1/doctor/health-alerts/{wo.workorder_id}/confirm", headers=doctor_h, json={}
    )
    assert resp.status_code == 200, resp.text

    resp = await api.get("/api/v1/doctor/health-alerts", headers=doctor_h)
    assert not any(it["workorder_id"] == str(wo.workorder_id) for it in resp.json())
    resp = await api.get("/api/v1/doctor/health-alerts?status=all", headers=doctor_h)
    hit = next(
        it for it in resp.json() if it["workorder_id"] == str(wo.workorder_id)
    )
    assert hit["status"] == "acknowledged" and hit["pending_site"] is None
