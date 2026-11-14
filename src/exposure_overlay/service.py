"""面向矢量资料的人口暴露统计组件。

典型用法::

    from src.exposure_overlay import ExposureService

    service = ExposureService.load("fixtures/sample.json")
    result = service.query(facility_ids=["west", "east"], thresholds=[1, 2])
    print(result.union, result.exact[1], result.exact[2])

    # 复用空间切片：先对设施并集切一次，再针对不同集合/等级阈值查询。
    at_level_1 = service.query_level(level_min=1, thresholds=[1, 2])

    # 面片台账用于复核。
    rows = service.ledger(result)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .arrangement import (
    Arrangement,
    CellArrangement,
    CellClip,
    Piece,
    UnknownCellError,
    build_arrangement,
    build_for_cells,
)
from .core import (
    ArrangementError,
    CellSpec,
    ExposureError,
    FacilitySpec,
    InvalidGeometryError,
    InvalidInputError,
    NormalizedGeometry,
    Scene,
    SchemaError,
    Tolerance,
    UnknownFacilityError,
    check_bundle,
    load_scene,
    normalize_geometry,
    parse_geometry,
    parse_scene,
    polygonal_parts,
    repair_bundle,
)
from .query import (
    QuerySpec,
    Coverage,
    compare_versions,
    ledger_for,
    ledger_totals,
    make_coverage,
    resolve_selection,
)
from .update import changed_cells, update_arrangement

__all__ = [
    "Arrangement",
    "ArrangementError",
    "CellArrangement",
    "CellClip",
    "CellSpec",
    "Coverage",
    "ExposureError",
    "ExposureService",
    "FacilitySpec",
    "InvalidGeometryError",
    "InvalidInputError",
    "NormalizedGeometry",
    "Piece",
    "QuerySpec",
    "Scene",
    "SchemaError",
    "Tolerance",
    "UnknownCellError",
    "UnknownFacilityError",
    "changed_cells",
    "check_bundle",
    "compare_versions",
    "ledger_for",
    "ledger_totals",
    "load_scene",
    "make_coverage",
    "normalize_geometry",
    "parse_geometry",
    "parse_scene",
    "polygonal_parts",
    "repair_bundle",
    "resolve_selection",
    "update_arrangement",
]


class ExposureService:
    """绑定资料、容差与已算切片的查询服务。

    同一份资料上的多个设施集合/等级阈值共享一次平面排列；``cached`` 记录每次
    切片的指纹与统计，便于审计复用范围。
    """

    def __init__(self, scene: Scene, tolerance: Tolerance | None = None):
        self.scene = scene
        self.tolerance = tolerance or Tolerance()
        self.cached: dict[tuple[str, ...], Arrangement] = {}

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_bundle(cls, bundle: dict[str, Any],
                    tolerance: Tolerance | None = None) -> "ExposureService":
        return cls(parse_scene(bundle, tolerance), tolerance)

    @classmethod
    def load(cls, path: str | Path, tolerance: Tolerance | None = None) -> "ExposureService":
        return cls(load_scene(path, tolerance), tolerance)

    # ------------------------------------------------------------------ 切片
    def arrangement(self, facility_ids: Iterable[str]) -> Arrangement:
        key = tuple(sorted(set(facility_ids)))
        if key not in self.cached:
            self.cached[key] = build_arrangement(self.scene, key, self.tolerance)
        return self.cached[key]

    def arrangement_for_cells(self, facility_ids: Iterable[str],
                              cell_ids: Iterable[str]) -> Arrangement:
        return build_for_cells(self.scene, facility_ids, cell_ids, self.tolerance)

    def precompute(self, selections: Iterable[Iterable[str]]) -> list[Arrangement]:
        """一次对多个设施集合切片：并集切一次，再逐个取子集。"""
        selections = [tuple(sorted(set(selection))) for selection in selections]
        union = tuple(sorted({ident for selection in selections for ident in selection}))
        base = self.arrangement(union)
        return [base.subset(selection) for selection in selections]

    # ------------------------------------------------------------------ 查询
    def query(self, facility_ids: Iterable[str] | None = None,
              level_min: int | None = None,
              thresholds: Iterable[int] = (1,)) -> Coverage:
        """查询设施集合或等级阈值下的并集/交集/恰好 k 人。

        ``facility_ids`` 为空时按 ``level_min`` 过滤全部设施；两者都给时，
        先按显式集合，再按等级过滤。
        """
        selected, unknown, excluded = resolve_selection(self.scene, facility_ids, level_min)
        explicit = tuple(set(facility_ids or ()))
        if explicit and not selected:
            # 显式点名了设施但一个都不存在：这是查询错误，而不是“没有覆盖”。
            raise UnknownFacilityError("未知设施：" + ", ".join(unknown))
        spec = QuerySpec.from_selection(selected, level_min, thresholds)
        if not spec.facility_ids:
            # 等级阈值可能排除全部设施；这是合法查询，全部人口未覆盖。
            return make_coverage(self.scene, _empty_arrangement(self.scene, self.tolerance),
                                 spec, unknown_ids=unknown, excluded_ids=excluded)
        arrangement = self.arrangement(spec.facility_ids)
        return make_coverage(self.scene, arrangement, spec,
                             unknown_ids=unknown, excluded_ids=excluded)

    def query_level(self, level_min: int = 1,
                    thresholds: Iterable[int] = (1,)) -> Coverage:
        return self.query(level_min=level_min, thresholds=thresholds)

    def query_scopes(self, facility_ids: Iterable[str] | None = None,
                     level_min: int | None = None,
                     thresholds: Iterable[int] = (1,)) -> Coverage:
        """把选中设施作为一个整体查询；与 ``query`` 等价，命名更明确。"""
        return self.query(facility_ids, level_min, thresholds)

    def query_reusing(self, facility_ids: Iterable[str],
                      thresholds: Iterable[int] = (1,),
                      base_ids: Iterable[str] | None = None) -> Coverage:
        """在已算出的更大切片上查子集；不会重算几何。"""
        base = self.arrangement(base_ids if base_ids is not None else facility_ids)
        selected, unknown, excluded = resolve_selection(self.scene, facility_ids, None)
        spec = QuerySpec.from_selection(selected, None, thresholds)
        if not set(spec.facility_ids) <= set(base.facility_ids):
            raise ArrangementError("基准切片未覆盖查询设施；请先调用 arrangement()")
        subset = base.subset(spec.facility_ids)
        return make_coverage(self.scene, subset, spec,
                             unknown_ids=unknown, excluded_ids=excluded)

    # ------------------------------------------------------------------ 台账
    def ledger(self, coverage: Coverage,
               facility_ids: Iterable[str] | None = None,
               include_geometry: bool = False) -> list[dict[str, Any]]:
        """复核台账；若给定设施集合则使用同一切片，否则按查询设施重建。"""
        arrangement = self.arrangement(facility_ids if facility_ids is not None
                                       else coverage.facility_ids)
        return ledger_for(arrangement, coverage, include_geometry)

    def ledger_totals(self, coverage: Coverage) -> dict[str, Any]:
        return ledger_totals(self.ledger(coverage))

    # ------------------------------------------------------------------ 版本
    def compare(self, new_service: "ExposureService",
                facility_ids: Iterable[str] | None = None,
                level_min: int | None = None,
                thresholds: Iterable[int] = (1,)) -> dict[str, Any]:
        """与另一版本资料对比同一查询。"""
        old_coverage = self.query(facility_ids, level_min, thresholds)
        new_coverage = new_service.query(facility_ids, level_min, thresholds)
        old_arrangement = self.arrangement(old_coverage.facility_ids)
        new_arrangement = new_service.arrangement(new_coverage.facility_ids)
        return compare_versions(old_arrangement, new_arrangement,
                                old_coverage, new_coverage)

    def changed_cells(self, new_service: "ExposureService",
                      facility_ids: Iterable[str] | None = None) -> dict[str, Any]:
        ids = tuple(facility_ids or [facility.id for facility in self.scene.facilities])
        return changed_cells(self.scene, new_service.scene, ids, self.tolerance)

    def update(self, new_service: "ExposureService",
               facility_ids: Iterable[str] | None = None,
               full_rebuild: bool = False) -> tuple[Arrangement, dict[str, Any]]:
        ids = tuple(facility_ids or [facility.id for facility in self.scene.facilities])
        base = self.arrangement(ids)
        return update_arrangement(self.scene, base, new_service.scene, ids,
                                  self.tolerance, full_rebuild)

    # ------------------------------------------------------------------ 输出
    def summary(self, coverage: Coverage) -> dict[str, Any]:
        return coverage.to_dict()


def _empty_arrangement(scene: Scene, tolerance: Tolerance) -> Arrangement:
    """用于“选中设施为空”的占位排列：没有任何切片，但保留版本与容差。"""
    return Arrangement(
        scene=scene,
        tolerance=tolerance,
        facility_ids=(),
        cells={},
        requested_ids=(),
        missing_ids=(),
        candidate_pairs=0,
        exact_pairs=0,
        degenerate_pairs=0,
        arrangement={"method": "empty_selection", "facilities": [], "cells_touched": 0,
                     "candidate_pairs": 0, "exact_pairs": 0, "degenerate_pairs": 0,
                     "tolerance": tolerance.to_dict()},
        cell_fingerprints={},
        facility_stats={},
        near_misses=(),
    )
