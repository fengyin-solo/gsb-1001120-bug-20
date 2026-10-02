"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

路段主数据改动需要同时落多张表（主表、巡查台账、病害空间索引、工程待办、车辆任务），
因此这里提供 transaction() 快照回滚与派生缓存登记，保证版本转换、空间索引更新与
旧缓存失效在同一事务里生效或一起回滚。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._versions: list[dict[str, Any]] = []
        self._derived: dict[str, Any] = {}
        self.reset()

    def reset(self) -> None:
        """按种子数据重建全部表、版本历史与派生缓存，测试与重启用。"""
        with self._lock:
            self._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
            self._versions = []
            self._derived = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨表写入的事务边界：任一环节抛错即整体回滚，派生缓存标记一并还原。"""
        with self._lock:
            tables_backup = copy.deepcopy(self._tables)
            versions_backup = copy.deepcopy(self._versions)
            derived_backup = dict(self._derived)
            try:
                yield
            except Exception:
                self._tables = tables_backup
                self._versions = versions_backup
                self._derived = derived_backup
                raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    # ---- 路段版本历史（版本转换台账，不属于业务模块，不进概览统计） ----

    def append_version(self, record: dict[str, Any]) -> None:
        self._versions.append(record)

    def versions_for(self, section_id: int) -> list[dict[str, Any]]:
        return [item for item in self._versions if int(item.get("路段id", 0)) == section_id]

    # ---- 派生缓存（地图区间、病害空间索引等），失效必须与主数据事务相符 ----

    def derived(self, name: str, builder: Callable[[], Any]) -> Any:
        with self._lock:
            if name not in self._derived:
                self._derived[name] = builder()
            return self._derived[name]

    def invalidate_derived(self, *names: str) -> None:
        with self._lock:
            for name in names:
                self._derived.pop(name, None)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
