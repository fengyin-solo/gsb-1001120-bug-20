"""路段管理接口：维护管养路段，覆盖登记、保存、设置施工、设置限行、恢复通行等动作。

注意路由顺序：/export、/map_intervals 等固定路径必须放在 /{entry_id} 之前，
否则会被当成路段 id 解析，直接 422，导出与地图区间永远读不到。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.road_section import RoadSectionService

router = APIRouter(prefix="/api/road_section", tags=["路段管理"])

service = RoadSectionService()

LIST_FIELDS = ["路段编号", "路段名称", "起止桩号", "道路等级", "车道数", "路面类型", "管养单位", "路段状态"]
STATUSES = ["正常", "施工", "限行", "封闭"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出路段管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "road_section", "total": total, "items": items}


@router.get("/map_intervals")
def map_intervals() -> dict[str, Any]:
    """地图区间：从派生索引读取，随路段主数据事务同步失效，不会读到旧边界。"""
    return {"items": service.map_intervals()}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路段编号检索"),
    status: str | None = Query(default=None, description="正常、施工、限行、封闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路段编号与状态过滤路段管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条管养路段明细；与列表共用同一序列化口径，不存在列表详情对不上。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"管养路段 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions")
def version_history(entry_id: int) -> dict[str, Any]:
    """路段版本台账：每次审定改动一条，重复提交不会重复记账。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"管养路段 {entry_id} 不存在或已归档")
    return {"items": service.version_history(entry_id)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条管养路段，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="管养路段已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """保存路段名称/起止桩号改动：版本锁校验通过后，同事务回写巡查台账、
    病害空间索引、工程待办与车辆任务；重复提交幂等，旧版本提交一律拒绝。"""
    operator = str(payload.values.get("操作人") or "值班管理员")
    entry, code, message = service.update_entry(
        entry_id,
        payload.values,
        expected_version=payload.version,
        operator=operator,
    )
    if code == "not_found":
        raise HTTPException(status_code=404, detail=message)
    if code == "conflict":
        raise HTTPException(status_code=409, detail=message)
    if code == "invalid":
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条管养路段执行设置施工、设置限行、恢复通行；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
