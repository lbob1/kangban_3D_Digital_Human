"""Day5 P2-9 覆盖率补强：Day2 时代路由/服务层冒烟（纯提升覆盖，无新逻辑）。

覆盖面：admin rooms/personas/accounts 全 CRUD + 三张授权关联表 +
elders 详情/更新/软删。全部复用既有 api/seed fixture（事务回滚隔离）。
"""

from tests.conftest import login


async def _admin(api, seed) -> dict:
    token = await login(api, "admin", seed["password"])
    return {"Authorization": f"Bearer {token}"}


# ---- rooms ----


async def test_admin_rooms_list_and_detail(api, seed):
    headers = await _admin(api, seed)
    resp = await api.get("/api/v1/admin/rooms", headers=headers)
    assert resp.status_code == 200
    room_id = seed["rooms"]["single"].room_id
    resp = await api.get(f"/api/v1/admin/rooms/{room_id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["room_id"] == str(room_id)


async def test_admin_update_room_bed_count(api, seed):
    headers = await _admin(api, seed)
    room_id = seed["rooms"]["other"].room_id
    resp = await api.put(
        f"/api/v1/admin/rooms/{room_id}",
        json={"bed_count": 2},  # RoomUpdate 为部分更新；bed_count 上限 2（schema 约束）
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["bed_count"] == 2


async def test_admin_delete_free_room_204(api, seed):
    headers = await _admin(api, seed)
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "C", "floor": 3, "room_number": "999", "bed_count": 1},
        headers=headers,
    )
    room_id = resp.json()["room_id"]
    resp = await api.delete(f"/api/v1/admin/rooms/{room_id}", headers=headers)
    assert resp.status_code == 204  # 无设备/长者占用 → 可停用；有占用见既有 409 用例


# ---- personas ----


async def test_admin_persona_crud_cycle(api, seed):
    headers = await _admin(api, seed)
    resp = await api.post(
        "/api/v1/admin/personas",
        json={"code": "persona-new", "name": "新人设", "greeting": "您好呀"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["persona_id"]

    assert (await api.get("/api/v1/admin/personas", headers=headers)).status_code == 200
    assert (await api.get(f"/api/v1/admin/personas/{pid}", headers=headers)).status_code == 200

    resp = await api.put(
        f"/api/v1/admin/personas/{pid}",
        json={"name": "改名人设", "is_active": False},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False

    # 未被长者引用 → 可删
    assert (await api.delete(f"/api/v1/admin/personas/{pid}", headers=headers)).status_code == 204


async def test_admin_delete_persona_in_use_soft_delete_idempotent(api, seed):
    """被引用人设删除 = 幂等软删（deactivate_persona 文档化设计，已引用长者保留）。"""
    headers = await _admin(api, seed)
    pid = seed["personas"]["on"].persona_id
    resp = await api.delete(f"/api/v1/admin/personas/{pid}", headers=headers)
    assert resp.status_code == 204
    resp = await api.get(f"/api/v1/admin/personas/{pid}", headers=headers)
    assert resp.status_code == 200 and resp.json()["is_active"] is False


# ---- accounts ----


async def test_admin_account_crud_and_lifecycle(api, seed):
    headers = await _admin(api, seed)
    resp = await api.post(
        "/api/v1/admin/accounts",
        json={
            "username": "nurse_new",
            "display_name": "新护工",
            "role": "nurse",
            "password": "ChangeMe_2026",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    aid = resp.json()["account_id"]
    assert "password_hash" not in resp.json()  # 响应绝不泄漏哈希

    assert (await api.get("/api/v1/admin/accounts", headers=headers)).status_code == 200
    assert (await api.get(f"/api/v1/admin/accounts/{aid}", headers=headers)).status_code == 200

    resp = await api.put(
        f"/api/v1/admin/accounts/{aid}",
        json={"display_name": "改名校护工", "phone": "13800000000"},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["phone"] == "13800000000"

    resp = await api.post(f"/api/v1/admin/accounts/{aid}/reset-password", headers=headers)
    assert resp.status_code == 200 and len(resp.json()["new_password"]) >= 8

    resp = await api.post(f"/api/v1/admin/accounts/{aid}/disable", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    resp = await api.post(f"/api/v1/admin/accounts/{aid}/enable", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "active"


async def test_admin_disable_admin_self_409(api, seed):
    headers = await _admin(api, seed)
    admin_id = seed["accounts"]["admin"].account_id
    resp = await api.post(f"/api/v1/admin/accounts/{admin_id}/disable", headers=headers)
    assert resp.status_code == 409


# ---- 授权关联表：buildings / doctors / families ----


async def test_admin_building_assignment_cycle(api, seed):
    headers = await _admin(api, seed)
    nurse_id = seed["accounts"]["nurse_b"].account_id
    resp = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(nurse_id), "building_code": "B"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    asg_id = resp.json()["assignment_id"]
    url = "/api/v1/admin/assignments/buildings"
    assert (await api.get(url, headers=headers)).status_code == 200
    assert (await api.delete(f"{url}/{asg_id}", headers=headers)).status_code == 204


async def test_admin_doctor_assignment_cycle(api, seed):
    headers = await _admin(api, seed)
    doctor_id = seed["accounts"]["doctor"].account_id
    elder_id = seed["elders"]["single"].elder_id
    resp = await api.post(
        "/api/v1/admin/assignments/doctors",
        json={"account_id": str(doctor_id), "elder_id": str(elder_id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    asg_id = resp.json()["assignment_id"]
    assert (await api.get("/api/v1/admin/assignments/doctors", headers=headers)).status_code == 200
    assert (
        await api.delete(f"/api/v1/admin/assignments/doctors/{asg_id}", headers=headers)
    ).status_code == 204


async def test_admin_family_link_cycle(api, seed):
    headers = await _admin(api, seed)
    family_id = seed["accounts"]["family"].account_id
    elder_id = seed["elders"]["d1"].elder_id
    resp = await api.post(
        "/api/v1/admin/assignments/families",
        json={"account_id": str(family_id), "elder_id": str(elder_id), "relation": "儿子"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    link_id = resp.json()["assignment_id"]
    assert (await api.get("/api/v1/admin/assignments/families", headers=headers)).status_code == 200
    assert (
        await api.delete(f"/api/v1/admin/assignments/families/{link_id}", headers=headers)
    ).status_code == 204


# ---- elders ----


async def test_admin_elder_detail_update_soft_delete(api, seed):
    headers = await _admin(api, seed)
    elder_id = seed["elders"]["other"].elder_id

    resp = await api.get(f"/api/v1/elders/{elder_id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["elder_id"] == str(elder_id)

    resp = await api.put(
        f"/api/v1/elders/{elder_id}", json={"name": "钱改名"}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["name"] == "钱改名"

    resp = await api.delete(f"/api/v1/elders/{elder_id}", headers=headers)
    assert resp.status_code == 204
    # 软删 = 置 discharged + 清床位（文档化设计）；详情对 admin 仍可见
    resp = await api.get(f"/api/v1/elders/{elder_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "discharged" and body["room_id"] is None


# ---- 补充面：auth/me、设备管理、健康档案汇总与导出 ----


async def test_admin_elders_list(api, seed):
    headers = await _admin(api, seed)
    resp = await api.get("/api/v1/elders", headers=headers)
    assert resp.status_code == 200
    assert any(e["name"] == "王单间" for e in resp.json())


async def test_admin_devices_surface(api, seed):
    headers = await _admin(api, seed)
    resp = await api.get("/api/v1/admin/devices", headers=headers)
    assert resp.status_code == 200
    dev_id = seed["devices"]["free"].device_id
    resp = await api.get(f"/api/v1/admin/devices/{dev_id}", headers=headers)
    assert resp.status_code == 200
    resp = await api.get(f"/api/v1/admin/devices/{dev_id}/tokens", headers=headers)
    assert resp.status_code == 200
    # 停用空闲设备（无活跃绑定）
    resp = await api.delete(f"/api/v1/admin/devices/{dev_id}", headers=headers)
    assert resp.status_code == 204


async def test_admin_health_audit_and_summaries_and_export(api, seed):
    headers = await _admin(api, seed)
    elder_id = seed["elders"]["single"].elder_id
    assert (
        await api.get(f"/api/v1/elders/{elder_id}/health/summaries", headers=headers)
    ).status_code == 200
    assert (
        await api.get(f"/api/v1/elders/{elder_id}/health/export", headers=headers)
    ).status_code == 200
    assert (await api.get("/api/v1/admin/health-audit", headers=headers)).status_code == 200
    assert (await api.get("/api/v1/admin/health-grants", headers=headers)).status_code == 200
    # noqa 缺省：上面四项均为只读面，状态码已各自断言


async def test_auth_me_endpoints(api, seed):
    headers = await _admin(api, seed)
    resp = await api.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    resp = await api.get("/api/v1/auth/me/user", headers=headers)
    assert resp.status_code == 200 and resp.json()["username"] == "admin"
