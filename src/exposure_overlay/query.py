"""查询语义层：覆盖集合、重数、并集/交集、守恒账目与版本对比。

所有数字都来自 ``Arrangement`` 的面片；查询本身只做隶属集合运算，不重算几何。
浮点容差只用于几何判定（是否实质交叠），从不用于逐片人数取整后再求和。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .arrangement import Arrangement, Piece
from .core import Scene, UnknownFacilityError

# 面积账目闭合的默认判定容差（平方米）。实际使用时会与 ``Tolerance`` 的
# 面积容差取较大者；它仅用于报告“是否闭合”，不参与人数分配。
CONSERVATION_AREA_TOLERANCE_M2 = 1e-6


@dataclass(frozen=True)
class QuerySpec:
    """一次查询：设施集合（显式或等级阈值）+ 目标重数集合。"""

    facility_ids: tuple[str, ...]
    level_min: int | None = None
    thresholds: tuple[int, ...] = (1,)

    @staticmethod
    def from_selection(facility_ids: Iterable[str] = (),
                       level_min: int | None = None,
                       thresholds: Iterable[int] = (1,)) -> "QuerySpec":
        thresholds = tuple(sorted({int(value) for value in thresholds}))
        if any(value < 1 for value in thresholds):
            raise ValueError("重数阈值必须为正整数")
        return QuerySpec(facility_ids=tuple(sorted(set(facility_ids))),
                         level_min=level_min,
                         thresholds=thresholds or (1,))

    def to_dict(self) -> dict[str, Any]:
        return {"facility_ids": list(self.facility_ids),
                "level_min": self.level_min,
                "thresholds": list(self.thresholds)}


def resolve_selection(scene: Scene,
                      facility_ids: Iterable[str] | None = None,
                      level_min: int | None = None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """把显式设施集合和等级阈值解析为最终选中集合。

    返回 ``(选中设施, 未知设施, 等级条件排除的设施)``；等级过滤为 ``level >= level_min``。
    """
    explicit = tuple(sorted(set(facility_ids or ())))
    unknown = tuple(sorted(ident for ident in explicit if ident not in scene.facility_by_id))
    candidates = tuple(ident for ident in explicit if ident in scene.facility_by_id)
    if not explicit:
        candidates = tuple(facility.id for facility in scene.facilities)
    if level_min is None:
        selected = candidates
        excluded = ()
    else:
        if type(level_min) is not int:
            raise ValueError("level_min 必须为整数")
        selected = tuple(ident for ident in candidates
                         if scene.facility_by_id[ident].level >= level_min)
        excluded = tuple(sorted(set(candidates) - set(selected)))
    return tuple(sorted(selected)), unknown, excluded


@dataclass(frozen=True)
class Coverage:
    """一次查询在面片层面的完整答案。"""

    version: str
    arrangement_fingerprint: str
    facility_ids: tuple[str, ...]
    level_min: int | None
    thresholds: tuple[int, ...]
    population_total: float
    uncovered: float
    at_least: dict[int, float]
    exact: dict[int, float]
    union: float
    intersection: float
    multiplicity_counts: dict[int, int]
    conservation: dict[str, Any]
    spatial: dict[str, Any]
    cells: tuple[dict[str, Any], ...]
    excluded_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()

    def piece_counts(self, piece: Piece) -> dict[str, Any]:
        """某个面片在本次查询中的归类：是否进入并集/交集、命中哪些阈值。"""
        k = len(frozenset(piece.facility_ids) & frozenset(self.facility_ids))
        return {
            "k": k,
            "in_union": k >= 1,
            "in_intersection": bool(self.facility_ids) and k == len(self.facility_ids),
            "exact_k": k,
            "at_least": [threshold for threshold in self.thresholds if k >= threshold],
        }

    def cells_by_cell(self) -> dict[str, dict[str, Any]]:
        return {row["cell_id"]: row for row in self.cells}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "arrangement_fingerprint": self.arrangement_fingerprint,
            "facility_ids": list(self.facility_ids),
            "level_min": self.level_min,
            "excluded_ids": list(self.excluded_ids),
            "unknown_ids": list(self.unknown_ids),
            "thresholds": list(self.thresholds),
            "population_total": self.population_total,
            "uncovered": self.uncovered,
            "at_least_k": {str(key): value for key, value in sorted(self.at_least.items())},
            "exact_k": {str(key): value for key, value in sorted(self.exact.items())},
            "union": self.union,
            "intersection": self.intersection,
            "multiplicity_piece_counts": {str(key): value
                                          for key, value in sorted(self.multiplicity_counts.items())},
            "conservation": self.conservation,
            "spatial": self.spatial,
            "cells": list(self.cells),
        }


def make_coverage(scene: Scene,
                  arrangement: Arrangement,
                  spec: QuerySpec,
                  unknown_ids: Iterable[str] = (),
                  excluded_ids: Iterable[str] = ()) -> Coverage:
    """在给定排列上执行查询。

    ``arrangement`` 必须涵盖 ``spec.facility_ids`` 的全部设施；若排列是用更大
    集合切的，调用 ``arrangement.subset(...)`` 即可，无需重算几何。
    """
    selected = tuple(sorted(set(spec.facility_ids)))
    missing = tuple(sorted(set(selected) - set(arrangement.facility_ids)))
    if missing:
        raise UnknownFacilityError("排列不含查询所需设施：" + ", ".join(missing))
    selected_set = frozenset(selected)
    if not selected_set:
        # 空选择是合法查询（例如等级阈值排除了全部设施）：全部人口未覆盖。
        return _empty_coverage(scene, spec, arrangement, unknown_ids, excluded_ids)

    exact_parts: dict[int, list[float]] = {key: [] for key in range(len(selected_set) + 1)}
    at_least_parts: dict[int, list[float]] = {threshold: [] for threshold in spec.thresholds}
    multiplicity_counts: dict[int, int] = {}
    union_parts: list[float] = []
    union_area_parts: list[float] = []
    intersection_parts: list[float] = []
    intersection_area_parts: list[float] = []
    uncovered_parts: list[float] = []
    cell_rows: list[dict[str, Any]] = []

    for cell in scene.cells:
        arranged = arrangement.cells.get(cell.id)
        pieces = arranged.pieces if arranged is not None else ()
        cell_union_parts: list[float] = []
        cell_intersection_parts: list[float] = []
        cell_exact_parts: dict[int, list[float]] = {}
        cell_uncovered_parts: list[float] = []
        counted_pieces = 0
        if pieces:
            for piece in pieces:
                k = len(frozenset(piece.facility_ids) & selected_set)
                if k == 0:
                    cell_uncovered_parts.append(piece.persons)
                    continue
                counted_pieces += 1
                union_parts.append(piece.persons)
                union_area_parts.append(piece.area)
                cell_union_parts.append(piece.persons)
                cell_exact_parts.setdefault(k, []).append(piece.persons)
                exact_parts[k].append(piece.persons)
                multiplicity_counts[k] = multiplicity_counts.get(k, 0) + 1
                if k == len(selected_set):
                    intersection_parts.append(piece.persons)
                    intersection_area_parts.append(piece.area)
                    cell_intersection_parts.append(piece.persons)
                for threshold in spec.thresholds:
                    if k >= threshold:
                        at_least_parts[threshold].append(piece.persons)
        else:
            # 排列未触及的单元：选中设施没有实质覆盖，整单元计入未覆盖。
            cell_uncovered_parts.append(cell.persons)
        uncovered_parts.extend(cell_uncovered_parts)
        cell_rows.append({
            "cell_id": cell.id,
            "cell_area": cell.geom.area,
            "cell_persons": cell.persons,
            "uncovered": math.fsum(cell_uncovered_parts),
            "union": math.fsum(cell_union_parts),
            "intersection": math.fsum(cell_intersection_parts),
            "exact_k": {str(key): math.fsum(values)
                        for key, values in sorted(cell_exact_parts.items())},
            "pieces_counted": counted_pieces,
        })

    exact = {key: math.fsum(values) for key, values in exact_parts.items()}
    at_least = {key: math.fsum(values) for key, values in at_least_parts.items()}
    uncovered = math.fsum(uncovered_parts)
    union_people = math.fsum(union_parts)
    union_area = math.fsum(union_area_parts)
    intersection_people = math.fsum(intersection_parts)
    intersection_area = math.fsum(intersection_area_parts)

    total = scene.person_total
    covered = union_people
    claimed = uncovered + covered
    error = claimed - total
    error_floor = 1e-9 * max(1.0, total)
    arranged_area = math.fsum(piece.area for cell in arrangement.cells.values()
                              for piece in cell.pieces)
    arranged_cell_area = math.fsum(cell.cell_area for cell in arrangement.cells.values())
    area_error = arranged_area - arranged_cell_area
    area_limit = max(CONSERVATION_AREA_TOLERANCE_M2,
                     arrangement.tolerance.area(*[cell.cell.geom.geometry
                                                  for cell in arrangement.cells.values()]
                                                or [None]))
    conservation = {
        "population_total": total,
        "uncovered": uncovered,
        "covered_union": covered,
        "sum_exact_k": math.fsum(exact.values()),
        "uncovered_plus_covered": claimed,
        "conservation_error": error,
        "conservation_error_relative": error / total if total else 0.0,
        "conservation_error_tolerance": error_floor,
        "conservation_within_tolerance": abs(error) <= error_floor,
        "covered_equals_sum_exact_k": abs(math.fsum(exact.values()) - covered) <= error_floor,
        "arranged_cell_area": arranged_cell_area,
        "arranged_piece_area": arranged_area,
        "area_conservation_error": area_error,
        "area_conservation_tolerance_m2": area_limit,
        "area_conservation_within_tolerance": abs(area_error) <= area_limit,
        "method": "exact piece sums with math.fsum; no per-piece rounding before summing",
    }

    spatial = {
        "candidate_pairs": arrangement.candidate_pairs,
        "exact_pairs": arrangement.exact_pairs,
        "degenerate_pairs": arrangement.degenerate_pairs,
        "candidate_definition": "STRtree 谓词候选对；命中不等于相交",
        "exact_definition": "精确求交后面积大于判定容差的设施×单元对",
        "near_misses": list(arrangement.near_misses),
        "facility_stats": dict(arrangement.facility_stats),
        "pieces_total": len(arrangement.pieces),
        "intersection_area": intersection_area,
        "union_area": union_area,
        "arrangement": dict(arrangement.arrangement),
    }

    return Coverage(
        version=scene.version,
        arrangement_fingerprint=arrangement.fingerprint(),
        facility_ids=selected,
        level_min=spec.level_min,
        thresholds=spec.thresholds,
        population_total=total,
        uncovered=uncovered,
        at_least=at_least,
        exact=exact,
        union=union_people,
        intersection=intersection_people,
        multiplicity_counts=multiplicity_counts,
        conservation=conservation,
        spatial=spatial,
        cells=tuple(cell_rows),
        excluded_ids=tuple(sorted(excluded_ids)),
        unknown_ids=tuple(sorted(unknown_ids)),
    )


def _empty_coverage(scene: Scene, spec: QuerySpec, arrangement: Arrangement,
                    unknown_ids: Iterable[str], excluded_ids: Iterable[str]) -> Coverage:
    """选中设施为空：全部人口未覆盖，并集/交集/各重数均为 0。"""
    total = scene.person_total
    error_floor = 1e-9 * max(1.0, total)
    zero = {key: 0.0 for key in range(0, 1)}
    conservation = {
        "population_total": total,
        "uncovered": total,
        "covered_union": 0.0,
        "sum_exact_k": 0.0,
        "uncovered_plus_covered": total,
        "conservation_error": 0.0,
        "conservation_error_relative": 0.0,
        "conservation_error_tolerance": error_floor,
        "conservation_within_tolerance": True,
        "covered_equals_sum_exact_k": True,
        "arranged_cell_area": 0.0,
        "arranged_piece_area": 0.0,
        "area_conservation_error": 0.0,
        "area_conservation_within_tolerance": True,
        "method": "empty selection",
    }
    rows = tuple({
        "cell_id": cell.id,
        "cell_area": cell.geom.area,
        "cell_persons": cell.persons,
        "uncovered": cell.persons,
        "union": 0.0,
        "intersection": 0.0,
        "exact_k": {},
        "pieces_counted": 0,
    } for cell in scene.cells)
    return Coverage(
        version=scene.version,
        arrangement_fingerprint=arrangement.fingerprint(),
        facility_ids=(),
        level_min=spec.level_min,
        thresholds=spec.thresholds,
        population_total=total,
        uncovered=total,
        at_least={threshold: 0.0 for threshold in spec.thresholds},
        exact=zero,
        union=0.0,
        intersection=0.0,
        multiplicity_counts={},
        conservation=conservation,
        spatial={"candidate_pairs": 0, "exact_pairs": 0, "degenerate_pairs": 0,
                 "near_misses": [], "facility_stats": {}, "pieces_total": 0,
                 "intersection_area": 0.0, "union_area": 0.0,
                 "arrangement": dict(arrangement.arrangement)},
        cells=rows,
        excluded_ids=tuple(sorted(excluded_ids)),
        unknown_ids=tuple(sorted(unknown_ids)),
    )


def ledger_for(arrangement: Arrangement, coverage: Coverage,
               include_geometry: bool = False) -> list[dict[str, Any]]:
    """面片台账：每个面片的人口贡献、隶属集合与本次查询归类。

    台账 ``persons`` 之和等于全部人口：排列单元内每个面片一行，未被排列触及的
    单元各以整单元一行补充（``piece_index`` 为 None）。``include_geometry=True``
    时附上 GeoJSON 面片，任何数字都可追到实际相交的空间片段。
    """
    from shapely.geometry import mapping

    rows: list[dict[str, Any]] = []
    for cell_id in sorted(arrangement.cells):
        cell = arrangement.cells[cell_id]
        for piece in cell.pieces:
            counts = coverage.piece_counts(piece)
            rows.append({
                "cell_id": cell_id,
                "piece_index": piece.index,
                "area": piece.area,
                "persons": piece.persons,
                "facility_ids": list(piece.facility_ids),
                "k": counts["k"],
                "in_union": counts["in_union"],
                "in_intersection": counts["in_intersection"],
                "at_least": counts["at_least"],
                "cell_persons": cell.cell.persons,
                "cell_area": cell.cell_area,
                "persons_per_m2": piece.persons / piece.area if piece.area else 0.0,
                **({"geometry": mapping(piece.geometry)} if include_geometry else {}),
            })
    for cell in arrangement.scene.cells:
        if cell.id not in arrangement.cells:
            rows.append({
                "cell_id": cell.id,
                "piece_index": None,
                "area": cell.geom.area,
                "persons": cell.persons,
                "facility_ids": [],
                "k": 0,
                "in_union": False,
                "in_intersection": False,
                "at_least": [],
                "cell_persons": cell.persons,
                "cell_area": cell.geom.area,
                "persons_per_m2": cell.persons / cell.geom.area if cell.geom.area else 0.0,
            })
    return rows


def ledger_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """从台账独立重算总量，用于与 ``Coverage`` 交叉复核。"""
    keys = sorted({row["k"] for row in rows})
    return {
        "persons": math.fsum(row["persons"] for row in rows),
        "union": math.fsum(row["persons"] for row in rows if row["in_union"]),
        "intersection": math.fsum(row["persons"] for row in rows if row["in_intersection"]),
        "uncovered": math.fsum(row["persons"] for row in rows if not row["in_union"]),
        "exact_k": {str(key): math.fsum(row["persons"] for row in rows if row["k"] == key)
                    for key in keys},
    }


def compare_versions(old: Arrangement, new: Arrangement,
                     old_coverage: Coverage, new_coverage: Coverage) -> dict[str, Any]:
    """设施换版后的影响差异，按人口单元定位。

    旧查询由旧 ``Arrangement`` 与旧 ``Coverage`` 保留，可继续按旧版本复现；
    新查询只使用新版本资料。两者人口基准应一致，否则差异还需并入人口更新。
    """
    before_rows = old_coverage.cells_by_cell()
    after_rows = new_coverage.cells_by_cell()
    touched = set(old.cells) | set(new.cells) | set(before_rows) | set(after_rows)
    cell_deltas: list[dict[str, Any]] = []
    for cell_id in sorted(touched):
        before = before_rows.get(cell_id, {})
        after = after_rows.get(cell_id, {})
        union_before = before.get("union", 0.0)
        union_after = after.get("union", 0.0)
        exact_before = before.get("exact_k", {})
        exact_after = after.get("exact_k", {})
        if (union_before == union_after
                and before.get("intersection", 0.0) == after.get("intersection", 0.0)
                and exact_before == exact_after
                and cell_id not in old.cells and cell_id not in new.cells):
            continue
        cell_deltas.append({
            "cell_id": cell_id,
            "union_before": union_before,
            "union_after": union_after,
            "union_delta": union_after - union_before,
            "intersection_before": before.get("intersection", 0.0),
            "intersection_after": after.get("intersection", 0.0),
            "intersection_delta": after.get("intersection", 0.0) - before.get("intersection", 0.0),
            "exact_k_before": exact_before,
            "exact_k_after": exact_after,
            "in_old_arrangement": cell_id in old.cells,
            "in_new_arrangement": cell_id in new.cells,
        })
    affected = [row for row in cell_deltas
                if row["union_delta"] != 0.0 or row["intersection_delta"] != 0.0
                or row["exact_k_before"] != row["exact_k_after"]]
    return {
        "old_version": old.scene.version,
        "new_version": new.scene.version,
        "old_fingerprint": old.fingerprint(),
        "new_fingerprint": new.fingerprint(),
        "old_query": old_coverage.to_dict(),
        "new_query": new_coverage.to_dict(),
        "union_delta": new_coverage.union - old_coverage.union,
        "intersection_delta": new_coverage.intersection - old_coverage.intersection,
        "uncovered_delta": new_coverage.uncovered - old_coverage.uncovered,
        "affected_cells": affected,
        "all_cell_deltas": cell_deltas,
        "old_result_reproducible": True,
        "note": "旧结果由旧排列与旧版本资料保留；新排列只使用新版本资料",
    }
