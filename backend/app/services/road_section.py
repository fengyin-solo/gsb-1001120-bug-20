"""路段管理业务规则：状态流转、字段校验与筛选口径都收在这里。

路段是主数据：路段名称或起止桩号改动后，要在同一事务里回写巡查台账、病害空间索引、
工程待办与车辆任务，并让地图区间等派生缓存同步失效。并发编辑走版本锁（乐观锁），
重复提交按改动摘要幂等，不会生成第二份路段版本，也不允许旧版本覆盖已确认边界。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services.stake import clip_interval, format_interval, parse_interval, subtract_interval
from app.store import store

MODULE = "road_section"
REQUIRED_FIELDS = ["路段编号", "路段名称", "起止桩号"]
STATUS_ORDER = ["正常", "施工", "限行", "封闭"]
ACTION_RULES = {"设置施工": "施工", "设置限行": "限行", "恢复通行": "正常"}
NEGATIVE_ACTIONS = []

MAP_INTERVAL_INDEX = "road_section_map_intervals"
PAVEMENT_SPATIAL_INDEX = "pavement_spatial_index"

# 巡查台账里已完成/已复核的记录属于历史，按原路段快照保留，不回写新边界
PATROL_HISTORY_STATUSES = {"已完成", "已复核"}
# 工程待办只覆盖未完工的单据；已竣工/已验收的工程是历史档案
PROJECT_PENDING_STATUSES = {"待开工", "施工中"}


def _change_digest(entry_id: int, name: str, stakes: str, announcement: str) -> str:
    """改动摘要：同一次改动重复提交时摘要不变，用于幂等拦截。"""
    raw = f"{entry_id}|{name}|{stakes}|{announcement}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class RoadSectionService:
    # ---- 读取：列表与详情共用同一序列化器，保证两边口径一致 ----

    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        interval = parse_interval(row.get("起止桩号"))
        item["版本"] = int(row.get("version", 1))
        item["地图区间"] = row.get("起止桩号") if interval else "桩号待核定"
        item["起点米"] = interval[0] if interval else None
        item["终点米"] = interval[1] if interval else None
        return item

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
        return [self._present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._present(row) if row is not None else None

    def map_intervals(self) -> list[dict[str, Any]]:
        """地图区间派生索引：随主数据事务失效重建，不会读到旧边界。"""

        def build() -> list[dict[str, Any]]:
            return [self._present(row) for row in store.rows(MODULE)]

        return store.derived(MAP_INTERVAL_INDEX, build)

    def version_history(self, entry_id: int) -> list[dict[str, Any]]:
        return store.versions_for(entry_id)

    # ---- 写入 ----

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        if parse_interval(values.get("起止桩号")) is None:
            return None, ["起止桩号（格式如 K0+000-K12+500，且起点小于终点）"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: str(values.get(field)).strip() for field in REQUIRED_FIELDS})
        entry["审定公告"] = str(values.get("审定公告") or "").strip() or "待审定"
        entry["version"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        with store.transaction():
            rows.append(entry)
            store.invalidate_derived(MAP_INTERVAL_INDEX)
        return self._present(entry), []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        expected_version: int | None,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str, str]:
        """保存路段主数据改动，并把结论事务性回写到下游台账与空间索引。

        返回 (明细, 结果码, 说明)；结果码：ok / noop / invalid / conflict / not_found。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, "not_found", f"管养路段 {entry_id} 不存在或已归档"

        name = str(values.get("路段名称") or "").strip()
        stakes = str(values.get("起止桩号") or "").strip()
        announcement = str(values.get("审定公告") or "").strip()
        missing = [field for field, val in (("路段名称", name), ("起止桩号", stakes), ("审定公告", announcement)) if not val]
        if missing:
            return None, "invalid", f"缺少必填字段：{'、'.join(missing)}"
        new_interval = parse_interval(stakes)
        if new_interval is None:
            return None, "invalid", "起止桩号格式不正确，应为 K0+000-K12+500 且起点小于终点"

        # 幂等：同一段改动重复提交直接确认，不再生成第二份路段版本
        digest = _change_digest(entry_id, name, stakes, announcement)
        if entry.get("改动摘要") == digest:
            return self._present(entry), "noop", "相同改动已生效，重复提交已忽略"

        old_name = str(entry.get("路段名称") or "")
        old_stakes = str(entry.get("起止桩号") or "")
        if (name, stakes, announcement) == (old_name, old_stakes, str(entry.get("审定公告") or "")):
            return self._present(entry), "noop", "内容无变化，未产生新版本"

        with store.transaction():
            # 版本锁复核放在事务锁内：两个并发请求不能都通过检查，
            # 旧版本再次提交不能覆盖已确认边界
            current_version = int(entry.get("version", 1))
            if entry.get("改动摘要") == digest:
                return self._present(entry), "noop", "相同改动已生效，重复提交已忽略"
            if expected_version is None or int(expected_version) != current_version:
                return None, "conflict", f"路段边界已被他人确认为版本 {current_version}，请刷新后基于最新版本修改"

            before = dict(entry)
            self._rewrite_patrol(entry, old_name, old_stakes, name, stakes)
            self._rewrite_pavement(entry, old_name, name, new_interval)
            self._rewrite_projects(old_name, name)
            self._rewrite_vehicles(old_name, name, stakes)

            entry["路段名称"] = name
            entry["起止桩号"] = stakes
            entry["审定公告"] = announcement
            entry["version"] = current_version + 1
            entry["改动摘要"] = digest

            store.append_version({
                "路段id": entry_id,
                "路段编号": entry.get("路段编号"),
                "版本号": entry["version"],
                "审定公告": announcement,
                "改动摘要": digest,
                "修改前": {"路段名称": old_name, "起止桩号": old_stakes, "审定公告": before.get("审定公告")},
                "修改后": {"路段名称": name, "起止桩号": stakes, "审定公告": announcement},
                "操作人": operator,
                "时间": datetime.now().isoformat(timespec="seconds"),
            })
            # 版本转换、空间索引更新与旧缓存失效同事务：到这里才统一作废旧缓存
            store.invalidate_derived(MAP_INTERVAL_INDEX, PAVEMENT_SPATIAL_INDEX)

        return self._present(entry), "ok", "路段主数据已更新，巡查台账、病害空间索引、工程待办与车辆任务已同步"

    # ---- 下游回写（只在事务里被调用） ----

    def _rewrite_patrol(self, entry: dict[str, Any], old_name: str, old_stakes: str, name: str, stakes: str) -> None:
        """巡查台账：在册任务跟随新边界，历史巡查按原路段快照保留。"""
        code = entry.get("路段编号")
        for row in store.rows("patrol"):
            linked = row.get("路段编号") == code or (not row.get("路段编号") and row.get("巡查路段") == old_name)
            if not linked:
                continue
            row["路段编号"] = code
            if row.get("status") in PATROL_HISTORY_STATUSES:
                row.setdefault("路段快照", f"{old_name} {old_stakes}")
            else:
                row["巡查路段"] = name
                row["桩号区间"] = stakes

    def _rewrite_pavement(self, entry: dict[str, Any], old_name: str, name: str, new_interval: tuple[float, float]) -> None:
        """病害空间索引：先改挂新路段名，再把跨出新区间的存量病害迁移拆分。"""
        code = entry.get("路段编号")
        rows = store.rows("pavement")
        others: list[tuple[dict[str, Any], tuple[float, float]]] = []
        for section in store.rows(MODULE):
            if int(section.get("id", 0)) == int(entry.get("id", 0)):
                continue
            interval = parse_interval(section.get("起止桩号"))
            if interval is not None:
                others.append((section, interval))

        def next_id() -> int:
            return max((int(row.get("id", 0)) for row in rows), default=0) + 1

        for row in list(rows):
            linked = row.get("路段编号") == code or (not row.get("路段编号") and row.get("所属路段") == old_name)
            if not linked:
                continue
            row["路段编号"] = code
            row["所属路段"] = name
            interval = parse_interval(row.get("起止桩号"))
            if interval is None:
                continue
            overlap = clip_interval(interval, new_interval)
            remainders = subtract_interval(interval, new_interval)
            if overlap is None and not remainders:
                continue
            if overlap is not None:
                # 区间收缩后仍有一部分落在本路段：原行收窄，余下部分迁出
                row["起止桩号"] = format_interval(*overlap)
            else:
                # 整段都跨出本路段：原行改挂到第一个承接路段
                target, piece = self._locate_piece(remainders[0], others)
                if target is None:
                    row["abnormal"] = True
                    continue
                row["路段编号"] = target.get("路段编号")
                row["所属路段"] = target.get("路段名称")
                row["起止桩号"] = format_interval(*piece)
                remainders = subtract_interval(interval, parse_interval(target.get("起止桩号")) or piece)
            self._split_remainders(row, remainders, others, rows, next_id)

    def _split_remainders(
        self,
        source: dict[str, Any],
        remainders: list[tuple[float, float]],
        others: list[tuple[dict[str, Any], tuple[float, float]]],
        rows: list[dict[str, Any]],
        next_id: Any,
    ) -> None:
        """把跨区余量按承接路段拆成新病害记录；无承接路段的挂回本路段并标异常。"""
        prefix = f"{source.get('病害编号')}-迁"
        for piece in remainders:
            target, clipped = self._locate_piece(piece, others)
            # 序号从已生成的拆分记录里数，递归拆分也不会重号
            seq = sum(1 for row in rows if str(row.get("病害编号", "")).startswith(prefix)) + 1
            split = dict(source)
            split["id"] = next_id()
            split["病害编号"] = f"{prefix}{seq}"
            rows.append(split)
            if target is None:
                split["路段编号"] = source.get("路段编号")
                split["所属路段"] = source.get("所属路段")
                split["起止桩号"] = format_interval(*piece)
                split["abnormal"] = True
            else:
                split["路段编号"] = target.get("路段编号")
                split["所属路段"] = target.get("路段名称")
                split["起止桩号"] = format_interval(*clipped)
                leftover = subtract_interval(piece, clipped)
                if leftover:
                    self._split_remainders(source, leftover, others, rows, next_id)

    @staticmethod
    def _locate_piece(
        piece: tuple[float, float],
        others: list[tuple[dict[str, Any], tuple[float, float]]],
    ) -> tuple[dict[str, Any] | None, tuple[float, float]]:
        """在相邻路段里找能承接这段区间的路段；找不到时返回 (None, 原区间)。"""
        for section, interval in others:
            clipped = clip_interval(piece, interval)
            if clipped is not None:
                return section, clipped
        return None, piece

    def _rewrite_projects(self, old_name: str, name: str) -> None:
        """工程待办：未完工单据跟随新路段名，已竣工/已验收的保持历史原样。"""
        for row in store.rows("project"):
            if row.get("施工路段") == old_name and row.get("status") in PROJECT_PENDING_STATUSES:
                row["施工路段"] = name

    def _rewrite_vehicles(self, old_name: str, name: str, stakes: str) -> None:
        """车辆任务：作业路段与任务区间跟随主数据新边界。"""
        for row in store.rows("vehicle"):
            if row.get("作业路段") == old_name:
                row["作业路段"] = name
                row["任务区间"] = stakes

    # ---- 状态流转 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
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
        return self._present(entry), f"管养路段已{action}"
