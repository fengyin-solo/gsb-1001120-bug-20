"""路段管理接口：维护管养路段，覆盖设置施工、设置限行、恢复通行等动作。

路段主数据保存走 PUT /api/road_section/{id}：版本锁 + 幂等 + 跨模块回写，
保存成功后巡查台账、病害空间索引、工程待办、车辆任务与地图区间读到同一份新边界。
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


@router.get("/map_intervals")
def map_intervals() -> dict[str, Any]:
    """地图区间：返回最新审定边界；主数据保存后旧缓存随事务失效，这里不会读到旧边界。"""
    return {"items": service.map_intervals()}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出路段管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "road_section", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条管养路段明细；与列表同源，不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"管养路段 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, Any]:
    """审定版本记录：现行桩号以最新一条审定公告为准，历史版本可追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"管养路段 {entry_id} 不存在或已归档")
    return {"items": service.list_versions(entry_id)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条管养路段，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="管养路段已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """保存路段主数据：版本锁校验通过后，在同一事务里完成版本转换、
    巡查台账/病害空间索引/工程待办/车辆任务回写与旧缓存失效。

    - base_version 与当前版本不一致时返回 409，再次提交不能覆盖已确认边界；
    - 同一 request_id 重复提交幂等返回，不会生成第二份路段版本。
    """
    entry, message, status = service.update_entry(
        entry_id,
        payload.values,
        base_version=payload.base_version,
        request_id=payload.request_id,
    )
    if status == 404:
        raise HTTPException(status_code=404, detail=message)
    if status == 409:
        raise HTTPException(status_code=409, detail=message)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条管养路段执行设置施工、设置限行、恢复通行；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
