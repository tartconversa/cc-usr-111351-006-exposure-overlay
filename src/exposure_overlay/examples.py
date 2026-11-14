"""内置合成场景与可复核示例。

场景全部使用米制等面积平面的互不重叠人口单元。``build_example_bundle``
对应需求中的基准例子：面积 100 的正方形人口 100，两影响矩形各占 60、
交叠 20，因此并集 100、恰好一处 80、恰好两处 20。
"""
from __future__ import annotations

from typing import Any

from shapely.affinity import translate
from shapely.geometry import box, mapping


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return mapping(box(x0, y0, x1, y1))


def _polygon(shell: list[tuple[float, float]],
             holes: list[list[tuple[float, float]]] | None = None) -> dict[str, Any]:
    from shapely.geometry import Polygon

    return mapping(Polygon(shell, holes or []))


def build_example_bundle(version: str = "example-v1") -> dict[str, Any]:
    """基准例子：并集 100、恰好一处 80、恰好两处 20。"""
    return {
        "schema_version": 1,
        "population": [
            {"id": "cell", "persons": 100, "geometry": _rect(0, 0, 10, 10)},
        ],
        "facilities": [
            {"id": "west", "level": 1, "geometry": _rect(0, 0, 6, 10)},
            {"id": "east", "level": 1, "geometry": _rect(4, 0, 10, 10)},
        ],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_hole_bundle(version: str = "hole-v1") -> dict[str, Any]:
    """带孔洞的影响区：孔洞内人口不计入任何覆盖。"""
    return {
        "schema_version": 1,
        "population": [
            {"id": "cell", "persons": 100, "geometry": _rect(0, 0, 10, 10)},
        ],
        "facilities": [
            {"id": "ringed", "level": 1,
             "geometry": _polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                                  [[(3, 3), (7, 3), (7, 7), (3, 7)]])},
        ],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_multipart_bundle(version: str = "multipart-v1") -> dict[str, Any]:
    """多部件影响区（两个分离矩形）与多部件人口单元。"""
    from shapely.geometry import MultiPolygon

    population = mapping(MultiPolygon([box(0, 0, 5, 10), box(20, 0, 25, 10)]))
    facility = mapping(MultiPolygon([box(0, 0, 4, 10), box(21, 0, 25, 10)]))
    return {
        "schema_version": 1,
        "population": [{"id": "cells", "persons": 80, "geometry": population}],
        "facilities": [{"id": "twin", "level": 1, "geometry": facility}],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_containment_bundle(version: str = "contain-v1") -> dict[str, Any]:
    """完全包含：影响区覆盖整个单元，且另一影响区完全落在其中。"""
    return {
        "schema_version": 1,
        "population": [
            {"id": "inner", "persons": 40, "geometry": _rect(2, 2, 8, 8)},
            {"id": "ring", "persons": 60,
             "geometry": _polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                                  [[(2, 2), (8, 2), (8, 8), (2, 8)]])},
        ],
        "facilities": [
            {"id": "outer", "level": 1, "geometry": _rect(-1, -1, 11, 11)},
            {"id": "overlay", "level": 2, "geometry": _rect(2, 2, 8, 8)},
        ],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_shared_edge_bundle(version: str = "edge-v1") -> dict[str, Any]:
    """仅共边：两个影响区在 x=5 处相接，重叠面积为 0。"""
    return {
        "schema_version": 1,
        "population": [
            {"id": "cell", "persons": 100, "geometry": _rect(0, 0, 10, 10)},
        ],
        "facilities": [
            {"id": "left", "level": 1, "geometry": _rect(0, 0, 5, 10)},
            {"id": "right", "level": 1, "geometry": _rect(5, 0, 10, 10)},
        ],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_shifted_bundle(version: str = "shifted-v1", dx: float = 1.5) -> dict[str, Any]:
    """设施小幅平移：east 右移 ``dx`` 米，用于演示增量更新。"""
    bundle = build_example_bundle(version)
    for record in bundle["facilities"]:
        if record["id"] == "east":
            record["geometry"] = mapping(translate(box(4, 0, 10, 10), xoff=dx))
    return bundle


def build_two_cell_bundle(version: str = "two-cell-v1", dx: float = 0.0) -> dict[str, Any]:
    """两个相邻人口单元；``dx`` 平移东侧设施，用于演示只重算受影响单元。

    设施初始完全落在 ``east_cell`` 内，小幅平移只影响这一个单元。
    """
    west_cell = {"id": "west_cell", "persons": 50, "geometry": _rect(0, 0, 10, 10)}
    east_cell = {"id": "east_cell", "persons": 50, "geometry": _rect(10, 0, 20, 10)}
    facility = box(12 + dx, 2, 18 + dx, 8)
    return {
        "schema_version": 1,
        "population": [west_cell, east_cell],
        "facilities": [{"id": "across", "level": 1, "geometry": mapping(facility)}],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


def build_nested_bundle(version: str = "nested-v1") -> dict[str, Any]:
    """三个设施两两重叠，用于演示恰好 k 处与交集。"""
    return {
        "schema_version": 1,
        "population": [
            {"id": "cell", "persons": 100, "geometry": _rect(0, 0, 10, 10)},
        ],
        "facilities": [
            {"id": "a", "level": 1, "geometry": _rect(0, 0, 6, 10)},
            {"id": "b", "level": 1, "geometry": _rect(3, 0, 9, 10)},
            {"id": "c", "level": 2, "geometry": _rect(5, 0, 10, 10)},
        ],
        "crs": {"kind": "equal_area_planar", "unit": "m"},
        "version": version,
    }


SCENARIOS = {
    "example": build_example_bundle,
    "hole": build_hole_bundle,
    "multipart": build_multipart_bundle,
    "containment": build_containment_bundle,
    "shared_edge": build_shared_edge_bundle,
    "nested": build_nested_bundle,
    "two_cell": build_two_cell_bundle,
}


__all__ = [
    "SCENARIOS",
    "build_containment_bundle",
    "build_example_bundle",
    "build_hole_bundle",
    "build_multipart_bundle",
    "build_nested_bundle",
    "build_shared_edge_bundle",
    "build_shifted_bundle",
    "build_two_cell_bundle",
]
