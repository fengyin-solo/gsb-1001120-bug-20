"""桩号工具：把「K12+300」这类桩号文本换算成可比较的米数，并支持区间解析。

路段主数据、病害空间索引、地图区间都靠它对齐口径，避免各模块各自解析导致边界对不上。
"""
from __future__ import annotations

import re

_STAKE_PATTERN = re.compile(r"^\s*[Kk]?(\d+)\+(\d{1,3})\s*$")
_INTERVAL_SPLIT = re.compile(r"[-—~～]+")


def parse_stake(text: object) -> float | None:
    """解析单个桩号（如 K12+300）为米数；无法识别时返回 None。"""
    match = _STAKE_PATTERN.match(str(text or ""))
    if not match:
        return None
    kilometers, meters = int(match.group(1)), int(match.group(2))
    if meters >= 1000:
        return None
    return kilometers * 1000 + meters


def format_stake(meters: float) -> str:
    """把米数格式化为标准桩号文本，保证回写后各模块看到的边界字面一致。"""
    total = int(round(meters))
    return f"K{total // 1000}+{total % 1000:03d}"


def parse_interval(text: object) -> tuple[float, float] | None:
    """解析起止桩号区间（如 K0+000-K12+500）；非法或起>=止时返回 None。"""
    parts = [part for part in _INTERVAL_SPLIT.split(str(text or "")) if part.strip()]
    if len(parts) != 2:
        return None
    start, end = parse_stake(parts[0]), parse_stake(parts[1])
    if start is None or end is None or start >= end:
        return None
    return start, end


def format_interval(start: float, end: float) -> str:
    """把区间米数格式化为「K0+000-K12+500」，供主数据与派生索引统一展示。"""
    return f"{format_stake(start)}-{format_stake(end)}"


def clip_interval(
    interval: tuple[float, float], bounds: tuple[float, float]
) -> tuple[float, float] | None:
    """求区间与边界的交集；没有重叠时返回 None。"""
    start, end = max(interval[0], bounds[0]), min(interval[1], bounds[1])
    if start >= end:
        return None
    return start, end


def subtract_interval(
    interval: tuple[float, float], bounds: tuple[float, float]
) -> list[tuple[float, float]]:
    """求区间落在边界以外的部分，最多拆出首尾两段；完全包含时返回空列表。"""
    pieces: list[tuple[float, float]] = []
    if interval[0] < bounds[0]:
        pieces.append((interval[0], min(interval[1], bounds[0])))
    if interval[1] > bounds[1]:
        pieces.append((max(interval[0], bounds[1]), interval[1]))
    return [piece for piece in pieces if piece[0] < piece[1]]
