"""路段主数据处理线回归测试。

覆盖缺陷单里的每个结论：
- 路段名称/起止桩号改动后，巡查台账、病害空间索引、工程待办、车辆任务、地图区间同步新边界
- 历史巡查按原路段快照保留
- 存量跨区重叠病害迁移拆分
- 重复提交幂等，不生成第二份路段版本
- 并发编辑版本锁，旧版本提交不能覆盖已确认边界
- 版本转换、空间索引更新与缓存失效事务相符（中途失败整体回滚）
- 列表与详情同一口径；/export 不被 /{entry_id} 吞掉
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.road_section import RoadSectionService
from app.store import store

NEW_NAME = "人民东路（调整）"
NEW_STAKES = "K0+000-K11+000"
NEW_ANNOUNCEMENT = "市养审〔2026〕32号"


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def save_payload(version: int = 1, **overrides) -> dict:
    values = {
        "路段名称": NEW_NAME,
        "起止桩号": NEW_STAKES,
        "审定公告": NEW_ANNOUNCEMENT,
    }
    values.update(overrides)
    return {"values": values, "version": version}


def test_update_rewrites_patrol_ledger(client: TestClient) -> None:
    response = client.put("/api/road_section/1", json=save_payload())
    assert response.status_code == 200, response.text

    patrols = {row["巡查编号"]: row for row in client.get("/api/patrol").json()["items"]}
    # 在册巡查任务跟随新边界
    assert patrols["PATR-0001"]["巡查路段"] == NEW_NAME
    assert patrols["PATR-0001"]["桩号区间"] == NEW_STAKES
    assert patrols["PATR-0002"]["巡查路段"] == NEW_NAME
    # 历史巡查按原路段快照保留
    assert patrols["PATR-0003"]["巡查路段"] == "人民东路"
    assert patrols["PATR-0003"]["桩号区间"] == "K0+000-K12+500"
    assert patrols["PATR-0003"]["路段快照"] == "人民东路 K0+000-K12+500"


def test_update_splits_cross_region_pavement(client: TestClient) -> None:
    response = client.put("/api/road_section/1", json=save_payload())
    assert response.status_code == 200, response.text

    rows = {row["病害编号"]: row for row in client.get("/api/pavement?size=50").json()["items"]}
    # 完全落在新区间内的病害只改挂新路段名
    assert rows["PAVE-0001"]["所属路段"] == NEW_NAME
    assert rows["PAVE-0001"]["起止桩号"] == "K3+200-K3+260"
    # 存量跨区病害：界内部分收窄留在原路段
    assert rows["PAVE-0002"]["所属路段"] == NEW_NAME
    assert rows["PAVE-0002"]["起止桩号"] == "K10+000-K11+000"
    # 越界部分迁移到相邻路段
    assert rows["PAVE-0002-迁1"]["所属路段"] == "解放大道"
    assert rows["PAVE-0002-迁1"]["路段编号"] == "ROAD-0002"
    assert rows["PAVE-0002-迁1"]["起止桩号"] == "K12+500-K14+000"
    # 无承接路段的余量挂回本路段并标异常，等待人工核定
    assert rows["PAVE-0002-迁2"]["所属路段"] == NEW_NAME
    assert rows["PAVE-0002-迁2"]["起止桩号"] == "K11+000-K12+500"
    assert rows["PAVE-0002-迁2"]["abnormal"] is True

    # 病害空间索引按新边界重建
    index = client.get("/api/pavement/spatial_index").json()["index"]
    assert {item["病害编号"] for item in index["ROAD-0002"]} == {"PAVE-0002-迁1"}


def test_update_rewrites_projects_and_vehicles(client: TestClient) -> None:
    response = client.put("/api/road_section/1", json=save_payload())
    assert response.status_code == 200, response.text

    projects = {row["工程编号"]: row for row in client.get("/api/project").json()["items"]}
    # 工程待办跟随新路段名
    assert projects["PROJ-0001"]["施工路段"] == NEW_NAME
    # 已竣工工程是历史档案，不改写
    assert projects["PROJ-0003"]["施工路段"] == "滨江路"

    vehicles = {row["车辆编号"]: row for row in client.get("/api/vehicle").json()["items"]}
    # 车辆任务读取新边界
    assert vehicles["VEHI-0001"]["作业路段"] == NEW_NAME
    assert vehicles["VEHI-0001"]["任务区间"] == NEW_STAKES
    assert vehicles["VEHI-0002"]["作业路段"] == NEW_NAME
    assert vehicles["VEHI-0003"]["作业路段"] == "滨江路"


def test_map_intervals_and_detail_follow_master(client: TestClient) -> None:
    client.put("/api/road_section/1", json=save_payload())

    intervals = {row["路段编号"]: row for row in client.get("/api/road_section/map_intervals").json()["items"]}
    assert intervals["ROAD-0001"]["地图区间"] == NEW_STAKES
    assert intervals["ROAD-0001"]["版本"] == 2

    # 列表与详情同一口径
    listed = next(row for row in client.get("/api/road_section").json()["items"] if row["id"] == 1)
    detail = client.get("/api/road_section/1").json()
    assert listed["地图区间"] == detail["地图区间"] == NEW_STAKES
    assert listed["版本"] == detail["版本"] == 2
    assert detail["审定公告"] == NEW_ANNOUNCEMENT


def test_duplicate_submit_does_not_create_second_version(client: TestClient) -> None:
    assert client.put("/api/road_section/1", json=save_payload()).status_code == 200
    # 页面未刷新时重复点击：带旧版本号的同一改动按幂等确认，不冲突也不重复记账
    again = client.put("/api/road_section/1", json=save_payload())
    assert again.status_code == 200
    assert "重复提交" in again.json()["message"]
    # 刷新后按新版本号再提交同一改动，同样幂等
    assert client.put("/api/road_section/1", json=save_payload(version=2)).status_code == 200

    detail = client.get("/api/road_section/1").json()
    assert detail["版本"] == 2
    versions = client.get("/api/road_section/1/versions").json()["items"]
    assert len(versions) == 1
    assert versions[0]["版本号"] == 2
    assert versions[0]["审定公告"] == NEW_ANNOUNCEMENT


def test_stale_version_cannot_overwrite_confirmed_boundary(client: TestClient) -> None:
    assert client.put("/api/road_section/1", json=save_payload()).status_code == 200
    # 并发编辑者拿着旧版本号提交另一套边界，必须被拒绝
    conflict = client.put(
        "/api/road_section/1",
        json=save_payload(version=1, 起止桩号="K0+000-K9+000", 审定公告="市养审〔2026〕40号"),
    )
    assert conflict.status_code == 409
    detail = client.get("/api/road_section/1").json()
    assert detail["起止桩号"] == NEW_STAKES
    assert detail["版本"] == 2
    # 缺版本号的新改动同样拒绝（同一改动的重复提交走幂等，不算冲突）
    missing = client.put(
        "/api/road_section/1",
        json={"values": {"路段名称": NEW_NAME, "起止桩号": "K0+000-K9+500", "审定公告": "市养审〔2026〕41号"}},
    )
    assert missing.status_code == 409


def test_invalid_stakes_rejected(client: TestClient) -> None:
    bad = client.put("/api/road_section/1", json=save_payload(起止桩号="人民路东段"))
    assert bad.status_code == 400
    detail = client.get("/api/road_section/1").json()
    assert detail["起止桩号"] == "K0+000-K12+500"


def test_transaction_rolls_back_on_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(record: dict) -> None:
        raise RuntimeError("模拟版本台账写入失败")

    monkeypatch.setattr(store, "append_version", boom)
    response = client.put("/api/road_section/1", json=save_payload())
    assert response.status_code == 500

    # 主数据与全部下游台账保持改动前状态
    detail = client.get("/api/road_section/1").json()
    assert detail["路段名称"] == "人民东路"
    assert detail["版本"] == 1
    patrols = {row["巡查编号"]: row for row in client.get("/api/patrol").json()["items"]}
    assert patrols["PATR-0001"]["巡查路段"] == "人民东路"
    pavements = client.get("/api/pavement?size=50").json()["items"]
    assert all(not row["病害编号"].endswith("-迁1") for row in pavements)
    projects = {row["工程编号"]: row for row in client.get("/api/project").json()["items"]}
    assert projects["PROJ-0001"]["施工路段"] == "人民东路"
    # 派生缓存未失效到错误状态：地图区间仍是旧边界且可用
    intervals = {row["路段编号"]: row for row in client.get("/api/road_section/map_intervals").json()["items"]}
    assert intervals["ROAD-0001"]["地图区间"] == "K0+000-K12+500"
    assert client.get("/api/road_section/1/versions").json()["items"] == []


def test_concurrent_edits_version_lock() -> None:
    """两个并发请求拿同一版本号改不同边界：只能成交一个，另一个被版本锁拦下。"""
    service = RoadSectionService()
    barrier = threading.Barrier(2)
    results: list[tuple[dict | None, str, str]] = []

    def edit(stakes: str, announcement: str) -> None:
        barrier.wait()
        results.append(
            service.update_entry(
                1,
                {"路段名称": "并发路", "起止桩号": stakes, "审定公告": announcement},
                expected_version=1,
            )
        )

    threads = [
        threading.Thread(target=edit, args=("K0+000-K9+000", "市养审〔2026〕61号")),
        threading.Thread(target=edit, args=("K0+000-K8+000", "市养审〔2026〕62号")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(code for _, code, _ in results) == ["conflict", "ok"]
    entry = store.find("road_section", 1)
    assert entry is not None and entry["version"] == 2
    assert len(store.versions_for(1)) == 1


def test_concurrent_identical_submit_stays_single_version() -> None:
    """同一改动并发重复提交：一次成交一次幂等，只有一份路段版本。"""
    service = RoadSectionService()
    barrier = threading.Barrier(2)
    results: list[tuple[dict | None, str, str]] = []
    values = {"路段名称": NEW_NAME, "起止桩号": NEW_STAKES, "审定公告": NEW_ANNOUNCEMENT}

    def edit() -> None:
        barrier.wait()
        results.append(service.update_entry(1, values, expected_version=1))

    threads = [threading.Thread(target=edit), threading.Thread(target=edit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(code for _, code, _ in results) == ["noop", "ok"]
    entry = store.find("road_section", 1)
    assert entry is not None and entry["version"] == 2
    assert len(store.versions_for(1)) == 1


def test_export_route_not_shadowed(client: TestClient) -> None:
    response = client.get("/api/road_section/export")
    assert response.status_code == 200
    assert response.json()["module"] == "road_section"
    assert response.json()["total"] == 3
def test_create_section_validates_stakes(client: TestClient) -> None:
    bad = client.post("/api/road_section", json={"values": {"路段编号": "ROAD-0009", "路段名称": "测试路", "起止桩号": "随便写写"}})
    assert bad.json()["ok"] is False
    ok = client.post(
        "/api/road_section",
        json={"values": {"路段编号": "ROAD-0009", "路段名称": "测试路", "起止桩号": "K0+000-K3+000", "审定公告": "市养审〔2026〕50号"}},
    )
    assert ok.json()["ok"] is True
    intervals = {row["路段编号"]: row for row in client.get("/api/road_section/map_intervals").json()["items"]}
    assert intervals["ROAD-0009"]["地图区间"] == "K0+000-K3+000"
