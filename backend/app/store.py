"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

路段主数据这类一写多模块的改动，通过 ``transaction()`` 串行化，并用 ``_cache``
承载派生数据（地图区间等），写入方在同一事务里失效旧缓存，保证读不到旧边界。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """分配模块内下一条记录的 id；调用方需已持有事务锁。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """在事务内向模块追加一条记录。"""
        self.rows(module).append(row)
        return row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """串行化跨模块写入：版本转换、空间索引更新与缓存失效同进同退。"""
        with self._lock:
            yield

    def cache_get(self, key: str) -> Any | None:
        with self._lock:
            return self._cache.get(key)

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = value

    def invalidate_cache(self, prefix: str | None = None) -> None:
        """失效派生数据缓存；prefix 为空时全部失效。"""
        with self._lock:
            if prefix is None:
                self._cache.clear()
                return
            for key in [key for key in self._cache if key.startswith(prefix)]:
                del self._cache[key]

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
