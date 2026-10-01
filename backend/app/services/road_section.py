"""路段管理业务规则：状态流转、字段校验与筛选口径都收在这里。

路段主数据处理线（保存名称/起止桩号）是平台主数据的源头，一次保存要在同一事务里完成：

1. 版本转换：路段行版本号 +1，并追加一条审定版本记录（现行桩号以最新审定公告为准）；
2. 结论回写：巡查台账、病害空间索引、工程待办、车辆任务（除雪作业）同步到新边界，
   历史巡查保留原路段快照不改写；
3. 存量跨区重叠的病害记录按新边界迁移拆分；
4. 旧派生缓存（地图区间等）随事务一并失效。

并发编辑采用版本锁：提交必须带上读取时的版本号，版本不一致即拒绝，
再次提交（同一 request_id）幂等返回首个结果，不会生成第二份路段版本。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "road_section"
VERSION_MODULE = "road_section_version"
REQUIRED_FIELDS = ["路段编号", "路段名称", "起止桩号"]
EDITABLE_FIELDS = ["路段名称", "起止桩号", "道路等级", "车道数", "路面类型", "管养单位"]
STATUS_ORDER = ["正常", "施工", "限行", "封闭"]
ACTION_RULES = {"设置施工": "施工", "设置限行": "限行", "恢复通行": "正常"}
NEGATIVE_ACTIONS = []

# 巡查台账里已闭环的状态属于历史巡查，按原路段快照保留，不回写新边界
PATROL_MODULE = "patrol"
PATROL_HISTORICAL_STATUSES = {"已完成", "已复核"}
# 病害空间索引
PAVEMENT_MODULE = "pavement"
# 工程待办：已竣工的属于历史，不回写
PROJECT_MODULE = "project"
PROJECT_ACTIVE_STATUSES = {"待开工", "施工中"}
# 车辆任务（除雪作业）：已完成的属于历史，不回写
WINTER_MODULE = "winter"
WINTER_ACTIVE_STATUSES = {"待作业", "作业中"}

MAP_INTERVALS_CACHE_KEY = "road_section:map_intervals"

_STAKE_TOKEN = re.compile(r"[Kk]?\s*(\d+(?:\.\d+)?)\s*(?:\+\s*(\d+(?:\.\d+)?))?")


def _stake_to_km(text: str) -> float | None:
    """把 'K12+300' / '12.3' 这类桩号解析成公里数。"""
    match = _STAKE_TOKEN.fullmatch(text.strip())
    if not match:
        return None
    whole = float(match.group(1))
    if match.group(2) is not None:
        return whole + float(match.group(2)) / 1000.0
    return whole


def parse_stake_range(text: Any) -> tuple[float, float] | None:
    """解析 'K0+000-K5+200' 形式的起止桩号，返回 (起点公里, 终点公里)。"""
    if not isinstance(text, str):
        return None
    parts = [part for part in re.split(r"[-~—–]", text) if part.strip()]
    if len(parts) != 2:
        return None
    start = _stake_to_km(parts[0])
    end = _stake_to_km(parts[1])
    if start is None or end is None or start >= end:
        return None
    return start, end


def format_stake(km: float) -> str:
    """把公里数格式化成 'K5+200' 桩号。"""
    whole = int(km)
    meters = round((km - whole) * 1000)
    if meters >= 1000:
        whole += 1
        meters -= 1000
    return f"K{whole}+{meters:03d}"


def _snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "路段编号": entry.get("路段编号"),
        "路段名称": entry.get("路段名称"),
        "起止桩号": entry.get("起止桩号"),
        "版本号": entry.get("version", 1),
    }


class RoadSectionService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路段编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 1
            store.insert(MODULE, entry)
            self._record_version(entry, None, "登记管养路段")
            store.invalidate_cache()
        return entry, []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        base_version: int | None,
        request_id: str | None,
    ) -> tuple[dict[str, Any] | None, str, int]:
        """保存路段主数据：版本锁 + 幂等 + 跨模块回写，全部在一个事务里。

        返回 (entry, message, status)：status 为 200/400/404/409，路由层据此响应。
        """
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}", 400
        new_range = parse_stake_range(values.get("起止桩号"))
        if new_range is None:
            return None, "起止桩号格式不正确，应为 K0+000-K5+200 且起点小于终点", 400
        if base_version is None:
            return None, "缺少版本号 base_version，请先读取最新路段详情再保存", 400

        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"管养路段 {entry_id} 不存在或已归档", 404

            applied: dict[str, Any] = entry.setdefault("已处理请求", {})
            if request_id and request_id in applied:
                kept = applied[request_id].get("版本号", entry.get("version", 1))
                return entry, f"重复提交已忽略：边界保持第 {kept} 版，未生成新版本", 200

            current_version = int(entry.get("version", 1))
            if int(base_version) != current_version:
                return (
                    None,
                    f"版本锁冲突：边界已审定到第 {current_version} 版，"
                    "请刷新后基于最新版本重试，再次提交不能覆盖已确认边界",
                    409,
                )

            old = _snapshot(entry)
            for field in EDITABLE_FIELDS:
                if field in values:
                    entry[field] = values.get(field)
            entry["version"] = current_version + 1
            new = _snapshot(entry)

            summary = self._propagate(entry, old, new)
            self._record_version(entry, old, "保存路段主数据", request_id=request_id)
            store.invalidate_cache()
            if request_id:
                applied[request_id] = {"版本号": entry["version"], "时间": _now()}

        message = f"管养路段已保存（第 {entry['version']} 版审定生效）：" + "，".join(summary)
        return entry, message, 200

    def _propagate(
        self,
        entry: dict[str, Any],
        old: dict[str, Any],
        new: dict[str, Any],
    ) -> list[str]:
        """把新边界回写到巡查台账、病害空间索引、工程待办与车辆任务。"""
        old_name = str(old.get("路段名称") or "")
        new_name = str(new.get("路段名称") or "")
        renamed = old_name != new_name

        patrol_updated, patrol_kept = self._sync_patrol(old, new, renamed)
        pavement_updated, pavement_split = self._sync_pavement(entry, old, new, renamed)
        project_updated = self._sync_named_rows(
            PROJECT_MODULE, "施工路段", old, new, PROJECT_ACTIVE_STATUSES, renamed
        )
        winter_updated = self._sync_named_rows(
            WINTER_MODULE, "作业路段", old, new, WINTER_ACTIVE_STATUSES, renamed
        )
        return [
            f"巡查台账回写 {patrol_updated} 条、历史快照保留 {patrol_kept} 条",
            f"病害空间索引更新 {pavement_updated} 条、跨区重叠迁移拆分 {pavement_split} 条",
            f"工程待办回写 {project_updated} 条",
            f"车辆任务回写 {winter_updated} 条",
            "旧缓存已失效",
        ]

    def _sync_patrol(
        self, old: dict[str, Any], new: dict[str, Any], renamed: bool
    ) -> tuple[int, int]:
        """巡查台账：在册巡查回写新边界，历史巡查保留原路段快照。"""
        updated = kept = 0
        for row in store.rows(PATROL_MODULE):
            if str(row.get("巡查路段") or "") != str(old.get("路段名称") or ""):
                continue
            if row.get("status") in PATROL_HISTORICAL_STATUSES:
                row.setdefault("路段快照", dict(old))
                kept += 1
                continue
            if renamed:
                row["巡查路段"] = new.get("路段名称")
            row["路段快照"] = dict(new)
            updated += 1
        return updated, kept

    def _sync_pavement(
        self,
        entry: dict[str, Any],
        old: dict[str, Any],
        new: dict[str, Any],
        renamed: bool,
    ) -> tuple[int, int]:
        """病害空间索引：随新边界重建；存量跨区重叠的记录迁移拆分。"""
        updated = split = 0
        section_range = parse_stake_range(new.get("起止桩号"))
        for row in list(store.rows(PAVEMENT_MODULE)):
            if str(row.get("所属路段") or "") != str(old.get("路段名称") or ""):
                continue
            disease_range = parse_stake_range(row.get("起止桩号"))
            if renamed:
                row["所属路段"] = new.get("路段名称")
            if disease_range is None or section_range is None:
                row["空间索引"] = self._spatial_index(entry, disease_range)
                updated += 1
                continue
            inside, remainders = _clip_range(disease_range, section_range)
            if inside is None:
                # 整段病害已落到新边界外：迁移到实际覆盖它的路段
                target = self._find_covering_section(disease_range, exclude_id=entry.get("id"))
                row["所属路段"] = target.get("路段名称") if target else "待迁移"
                row["空间索引"] = self._spatial_index(target, disease_range)
                row["迁移备注"] = "边界调整后整段迁出原路段"
                updated += 1
                continue
            if not remainders:
                row["空间索引"] = self._spatial_index(entry, disease_range)
                updated += 1
                continue
            # 跨区重叠：原记录收敛到界内部分，界外部分拆成新记录并迁移
            row["起止桩号"] = f"{format_stake(inside[0])}-{format_stake(inside[1])}"
            row["空间索引"] = self._spatial_index(entry, inside)
            row["迁移备注"] = "跨区重叠按新边界拆分，界外部分已迁移"
            updated += 1
            for remainder in remainders:
                target = self._find_covering_section(remainder, exclude_id=entry.get("id"))
                clone = {
                    key: value for key, value in row.items() if key not in {"id", "已处理请求"}
                }
                clone["id"] = store.next_id(PAVEMENT_MODULE)
                clone["病害编号"] = f"{row.get('病害编号')}-S{split + 1}"
                clone["起止桩号"] = f"{format_stake(remainder[0])}-{format_stake(remainder[1])}"
                clone["所属路段"] = target.get("路段名称") if target else "待迁移"
                clone["空间索引"] = self._spatial_index(target, remainder)
                clone["迁移备注"] = f"自 {old.get('路段名称')} 跨区重叠拆分迁入"
                store.insert(PAVEMENT_MODULE, clone)
                split += 1
        return updated, split

    def _sync_named_rows(
        self,
        module: str,
        field: str,
        old: dict[str, Any],
        new: dict[str, Any],
        active_statuses: set[str],
        renamed: bool,
    ) -> int:
        """工程待办 / 车辆任务：在办记录回写新边界快照，已闭环的保留历史名称。"""
        old_name = str(old.get("路段名称") or "")
        updated = 0
        for row in store.rows(module):
            if str(row.get(field) or "") != old_name:
                continue
            if row.get("status") not in active_statuses:
                continue
            if renamed:
                row[field] = new.get("路段名称")
            row["路段快照"] = dict(new)
            updated += 1
        return updated

    def _find_covering_section(
        self, stake_range: tuple[float, float], *, exclude_id: Any
    ) -> dict[str, Any] | None:
        """在现行路段里找一条能覆盖给定桩号区间的（用于迁移拆分落点）。"""
        for row in store.rows(MODULE):
            if row.get("id") == exclude_id:
                continue
            bounds = parse_stake_range(row.get("起止桩号"))
            if bounds and bounds[0] <= stake_range[0] and stake_range[1] <= bounds[1]:
                return row
        return None

    @staticmethod
    def _spatial_index(section: dict[str, Any] | None, stake_range: tuple[float, float] | None) -> str:
        """病害空间索引键：路段编号 + 公里区间，供地图区间与检索对齐。"""
        code = section.get("路段编号") if section else "UNASSIGNED"
        if stake_range is None:
            return f"{code}:unknown"
        return f"{code}:{stake_range[0]:.3f}-{stake_range[1]:.3f}"

    def _record_version(
        self,
        entry: dict[str, Any],
        old: dict[str, Any] | None,
        note: str,
        *,
        request_id: str | None = None,
    ) -> None:
        """追加审定版本记录：现行桩号以最新一条审定公告为准。"""
        store.insert(VERSION_MODULE, {
            "id": store.next_id(VERSION_MODULE),
            "路段编号": entry.get("路段编号"),
            "版本号": entry.get("version", 1),
            "路段名称": entry.get("路段名称"),
            "起止桩号": entry.get("起止桩号"),
            "变更前": old,
            "审定时间": _now(),
            "说明": note,
            "请求标识": request_id,
        })

    def list_versions(self, entry_id: int) -> list[dict[str, Any]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return []
        code = entry.get("路段编号")
        versions = [row for row in store.rows(VERSION_MODULE) if row.get("路段编号") == code]
        return sorted(versions, key=lambda row: int(row.get("版本号", 0)))

    def map_intervals(self) -> list[dict[str, Any]]:
        """地图区间：派生自最新审定边界，随主数据保存一并失效重建。"""
        cached = store.cache_get(MAP_INTERVALS_CACHE_KEY)
        if cached is not None:
            return cached
        intervals: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            bounds = parse_stake_range(row.get("起止桩号"))
            intervals.append({
                "id": row.get("id"),
                "路段编号": row.get("路段编号"),
                "路段名称": row.get("路段名称"),
                "起止桩号": row.get("起止桩号"),
                "起点公里": bounds[0] if bounds else None,
                "终点公里": bounds[1] if bounds else None,
                "状态": row.get("status"),
                "版本号": row.get("version", 1),
            })
        store.cache_put(MAP_INTERVALS_CACHE_KEY, intervals)
        return intervals

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"管养路段 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于路段管理可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            store.invalidate_cache()
        return entry, f"管养路段已{action}"


def _clip_range(
    disease: tuple[float, float], section: tuple[float, float]
) -> tuple[tuple[float, float] | None, list[tuple[float, float]]]:
    """用路段区间裁剪病害区间，返回 (界内部分, 界外部分列表)。"""
    start, end = disease
    lo, hi = section
    inside: tuple[float, float] | None = None
    if end > lo and start < hi:
        inside = (max(start, lo), min(end, hi))
    remainders: list[tuple[float, float]] = []
    if start < lo and end > lo:
        remainders.append((start, min(end, lo)))
    if end > hi and start < hi:
        remainders.append((max(start, hi), end))
    if inside is None:
        remainders = [(start, end)]
    return inside, remainders


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
