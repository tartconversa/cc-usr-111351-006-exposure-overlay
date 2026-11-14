"""一次计算、多次查询的空间切片索引。

做法：把（人口单元 × 影响面片段）一次性排列成不可再分的“面片”。

1. 对每个参与切片的设施，把它的影响面与所属人口单元求交，得到片段（clip）。
   相交面积只用于“是否有实质交叠”的拓扑判定，人口分配始终使用片段面积。
2. 每个单元内，把单元边界和所有片段边界合并，做平面图 ``polygonize``。
   结果面片按构造满足：要么完整落在某片段内，要么与任何片段只共享边界。
3. 用面片的有理点对片段做 ``covers`` 复核（STRtree 只用于候选筛选，
   命中本身不是相交证据），得到面片的设施隶属集合。
4. 面积与人数在面片层面一次性累加；重数维度由面片集合精确求和。

复用：``Arrangement.subset`` 按隶属集合做位运算，不重算几何。因此同一批
查询可以先对设施并集切一次，再对任意子集/等级阈值回答。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import shapely
from shapely.ops import polygonize_full

from .core import (
    ArrangementError,
    CellSpec,
    Scene,
    Tolerance,
    UnknownFacilityError,
    polygonal_parts,
)


class UnknownCellError(UnknownFacilityError):
    """查询引用了资料中不存在的人口单元。"""


def _mask(*facility_ids: str) -> tuple[str, ...]:
    return tuple(sorted(facility_ids))


@dataclass(frozen=True)
class CellClip:
    """某设施在某人口单元内的影响片段。"""

    facility_id: str
    cell_id: str
    area: float
    geometry: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class Piece:
    """单元内不可再分的面片；``facility_ids`` 是切削所用设施的隶属集合。"""

    cell_id: str
    index: int
    area: float
    persons: float
    facility_ids: tuple[str, ...]
    geometry: Any = field(repr=False, compare=False)

    def to_dict(self, include_geometry: bool = True) -> dict[str, Any]:
        result = {
            "cell_id": self.cell_id,
            "piece_index": self.index,
            "area": self.area,
            "persons": self.persons,
            "facility_ids": list(self.facility_ids),
            "k": len(self.facility_ids),
        }
        if include_geometry:
            from shapely.geometry import mapping

            result["geometry"] = mapping(self.geometry)
        return result

    def key(self) -> tuple[str, int]:
        return self.cell_id, self.index


@dataclass(frozen=True)
class CellArrangement:
    """一个受影响人口单元的切片结果与单元内守恒量。"""

    cell: CellSpec
    clips: tuple[CellClip, ...]
    pieces: tuple[Piece, ...]
    uncovered_persons: float
    multiplicity_persons: tuple[float, ...]
    total_persons: float
    clipped_area: float
    cell_area: float

    def piece_by_index(self, index: int) -> Piece:
        return self.pieces[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell.id,
            "cell_area": self.cell_area,
            "cell_persons": self.cell.persons,
            "clipped_area": self.clipped_area,
            "uncovered_persons": self.uncovered_persons,
            "multiplicity_persons": list(self.multiplicity_persons),
            "total_persons": self.total_persons,
            "pieces": [piece.to_dict() for piece in self.pieces],
            "clips": [{"facility_id": clip.facility_id, "cell_id": clip.cell_id,
                       "area": clip.area} for clip in self.clips],
        }


@dataclass
class Arrangement:
    """一次空间计算的产物，可复用、可序列化、可与其它版本对比。"""

    scene: Scene
    tolerance: Tolerance
    facility_ids: tuple[str, ...]
    cells: dict[str, CellArrangement]
    requested_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    candidate_pairs: int
    exact_pairs: int
    degenerate_pairs: int
    arrangement: dict[str, Any]
    cell_fingerprints: dict[str, str]
    facility_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    near_misses: tuple[dict[str, Any], ...] = ()

    @property
    def pieces(self) -> tuple[Piece, ...]:
        return tuple(piece for cell in self.cells.values() for piece in cell.pieces)

    def fingerprint(self) -> str:
        """面片内容指纹：与切片路径、面片编号和候选统计无关。

        用于判断“增量结果是否与整体重切等价”。面片按几何/隶属/人口排序后计入，
        因此复用单元与重切单元先后顺序不影响指纹。
        """
        hasher = hashlib.sha256()
        records = []
        for cell_id in sorted(self.cells):
            for piece in self.cells[cell_id].pieces:
                records.append((piece.cell_id, piece.facility_ids,
                                round(piece.area, 12), round(piece.persons, 12),
                                shapely.to_wkb(piece.geometry)))
        for record in sorted(records, key=repr):
            hasher.update(repr(record).encode())
        hasher.update(repr(self.facility_ids).encode())
        return hasher.hexdigest()

    def to_dict(self, include_geometry: bool = True) -> dict[str, Any]:
        return {
            "version": self.scene.version,
            "facility_ids": list(self.facility_ids),
            "cells": {cell_id: cell.to_dict() for cell_id, cell in self.cells.items()},
            "requested_ids": list(self.requested_ids),
            "missing_ids": list(self.missing_ids),
            "candidate_pairs": self.candidate_pairs,
            "exact_pairs": self.exact_pairs,
            "degenerate_pairs": self.degenerate_pairs,
            "arrangement": self.arrangement,
            "facility_stats": self.facility_stats,
            "near_misses": list(self.near_misses),
            "fingerprint": self.fingerprint(),
            "tolerance": self.tolerance.to_dict(),
        }

    def membership_totals(self) -> dict[str, dict[str, tuple[float, float]]]:
        """按 (单元, 隶属集合) 聚合面积与人数，与面片如何切分无关。

        子集查询复用父切片的边界，面片可能比独立重切更细；但同一隶属集合的
        面积/人数总和相同。这个映射是判断“等价索引”的规范形式。
        """
        result: dict[str, dict[str, tuple[float, float]]] = {}
        for cell_id in sorted(self.cells):
            cell = self.cells[cell_id]
            aggregates: dict[str, list[float]] = {}
            for piece in cell.pieces:
                key = ",".join(piece.facility_ids)
                bucket = aggregates.setdefault(key, [0.0, 0.0])
                bucket[0] += piece.area
                bucket[1] += piece.persons
            result[cell_id] = {key: (round(value[0], 9), round(value[1], 9))
                               for key, value in sorted(aggregates.items())}
        return result

    def subset(self, selected: Iterable[str]) -> "Arrangement":
        """在已算出的切片上取设施子集；几何不重算，只裁剪隶属集合。

        因为所有参与设施的边界都已参与平面排列，面片对任何子集都是
        “整片在内或整片在外”，所以按隶属集合求交是正确的。
        """
        selected = frozenset(selected)
        unknown = tuple(sorted(ident for ident in selected if ident not in self.facility_ids
                               and ident not in self.scene.facility_by_id))
        if not self.facility_ids or not selected:
            raise UnknownFacilityError("查询未包含任何设施")
        for ident in selected:
            if ident not in self.scene.facility_by_id:
                unknown_ids = tuple(sorted(set(unknown) | {ident}))
                raise UnknownFacilityError("未知设施：" + ", ".join(unknown_ids))
        if not selected <= set(self.facility_ids):
            raise ArrangementError("切片尚未覆盖这些设施，需重新计算：" +
                                   ", ".join(sorted(selected - set(self.facility_ids))))

        cells: dict[str, CellArrangement] = {}
        for cell_id, cell in self.cells.items():
            pieces = [Piece(cell_id=piece.cell_id, index=piece.index, area=piece.area,
                            persons=piece.persons,
                            facility_ids=_mask(*[ident for ident in piece.facility_ids
                                                 if ident in selected]),
                            geometry=piece.geometry)
                      for piece in cell.pieces]
            if not any(piece.facility_ids for piece in pieces):
                # 该单元不受子集覆盖：从切片中移除，查询时整单元计入未覆盖。
                continue
            rearranged = _recount(cell.cell, tuple(clip for clip in cell.clips
                                                   if clip.facility_id in selected),
                                  tuple(pieces), cell.cell_area)
            cells[cell_id] = rearranged
        stats = {ident: dict(self.facility_stats.get(ident, {"candidates": 0, "exact": 0,
                                                            "degenerate": 0}))
                 for ident in selected}
        arrangement_meta = dict(self.arrangement)
        arrangement_meta.update({
            "facilities": list(_mask(*selected)),
            "selected_levels": {ident: self.scene.facility_by_id[ident].level for ident in selected},
            "cells_touched": sum(1 for cell in cells.values() if cell.clipped_area > 0),
            "candidate_pairs": sum(stats[ident]["candidates"] for ident in selected),
            "exact_pairs": sum(stats[ident]["exact"] for ident in selected),
            "degenerate_pairs": sum(stats[ident]["degenerate"] for ident in selected),
            "derived_from_fingerprint": self.fingerprint(),
        })
        return Arrangement(
            scene=self.scene,
            tolerance=self.tolerance,
            facility_ids=_mask(*selected),
            cells={cell_id: cells[cell_id] for cell_id in sorted(cells)},
            requested_ids=_mask(*selected),
            missing_ids=self.missing_ids,
            candidate_pairs=sum(stats[ident]["candidates"] for ident in selected),
            exact_pairs=sum(stats[ident]["exact"] for ident in selected),
            degenerate_pairs=sum(stats[ident]["degenerate"] for ident in selected),
            arrangement=arrangement_meta,
            cell_fingerprints={cell_id: self.scene.cell_by_id[cell_id].geom.fingerprint
                               for cell_id in sorted(cells)},
            facility_stats=stats,
            near_misses=tuple(item for item in self.near_misses if item["facility_id"] in selected),
        )


def _recount(cell: CellSpec, clips: Sequence[CellClip], pieces: Sequence[Piece],
             cell_area: float) -> CellArrangement:
    """按已有面片重算单元内未覆盖与各重数人口。"""
    buckets: list[list[float]] = [[] for _ in range(len(clips) + 1)]
    for piece in pieces:
        buckets[len(piece.facility_ids)].append(piece.persons)
    multiplicities = [math.fsum(values) for values in buckets]
    total_persons = math.fsum(piece.persons for piece in pieces)
    clipped_area = math.fsum(piece.area for piece in pieces if piece.facility_ids)
    if abs(total_persons - cell.persons) > 1e-9 * max(1.0, cell.persons):
        raise ArrangementError(f"单元 {cell.id} 的子集重算后人数不守恒")
    return CellArrangement(cell=cell, clips=tuple(clips), pieces=tuple(pieces),
                           uncovered_persons=multiplicities[0],
                           multiplicity_persons=tuple(multiplicities),
                           total_persons=total_persons, clipped_area=clipped_area,
                           cell_area=cell_area)


def _canonical(geometry: Any, tolerance: Tolerance) -> Any:
    parts = polygonal_parts(geometry)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return shapely.union_all(parts, grid_size=tolerance.grid_size_m)


def _clip_area(geometry: Any, tolerance: Tolerance) -> float:
    canonical = _canonical(geometry, tolerance)
    if canonical is None:
        return 0.0
    area = float(shapely.area(canonical))
    return area if area > tolerance.area(canonical) else 0.0


def build_arrangement(scene: Scene,
                      facility_ids: Iterable[str],
                      tolerance: Tolerance | None = None,
                      only_cells: Iterable[str] | None = None) -> Arrangement:
    """对给定设施集合做一次完整切片。

    ``only_cells`` 限定只计算这些人口单元（增量更新用）；未列出的单元视为
    不受该设施集合影响，不出现在结果中。
    """
    return _build(scene, facility_ids, tolerance, only_cells)[0]


def build_for_cells(scene: Scene,
                    facility_ids: Iterable[str],
                    cell_ids: Iterable[str],
                    tolerance: Tolerance | None = None) -> Arrangement:
    """只对指定单元切片；用于设施换版后的增量更新。"""
    return _build(scene, facility_ids, tolerance, cell_ids)[0]


def resolve_facilities(scene: Scene, facility_ids: Iterable[str]) -> tuple[list[Any], tuple[str, ...]]:
    requested = _mask(*facility_ids)
    facilities = []
    missing: list[str] = []
    for ident in requested:
        facility = scene.facility_by_id.get(ident)
        if facility is None:
            missing.append(ident)
        else:
            facilities.append(facility)
    return facilities, tuple(missing)


def _build(scene: Scene,
           facility_ids: Iterable[str],
           tolerance: Tolerance | None,
           only_cells: Iterable[str] | None) -> tuple[Arrangement, dict[str, list[tuple[str, Any, float]]]]:
    tolerance = tolerance or Tolerance()
    requested = _mask(*facility_ids)
    facilities, missing = resolve_facilities(scene, requested)
    if not facilities:
        raise UnknownFacilityError("查询未包含任何已知设施：" + ", ".join(missing) if missing
                                   else "查询未包含任何设施")

    if only_cells is None:
        selected_cells = list(scene.cells)
    else:
        wanted = set(only_cells)
        unknown_cells = sorted(wanted - set(scene.cell_by_id))
        if unknown_cells:
            raise UnknownCellError("未知人口单元：" + ", ".join(unknown_cells))
        selected_cells = [cell for cell in scene.cells if cell.id in wanted]

    cell_geometries = [cell.geom.geometry for cell in selected_cells]
    cell_tree = shapely.STRtree(cell_geometries) if cell_geometries else None
    candidate_pairs = 0
    exact_pairs = 0
    degenerate_pairs = 0
    facility_stats: dict[str, dict[str, int]] = {
        facility.id: {"candidates": 0, "exact": 0, "degenerate": 0} for facility in facilities}
    pairs_by_cell: dict[str, list[tuple[str, Any, float]]] = {}
    near_misses: list[dict[str, Any]] = []
    for facility in facilities:
        # 第 1 步：近邻索引只给候选；命中本身不构成“相交”。
        candidates = cell_tree.query(facility.geom.geometry, predicate="intersects") \
            if cell_tree is not None else []
        candidate_pairs += len(candidates)
        facility_stats[facility.id]["candidates"] = int(len(candidates))
        for cell_index in sorted(int(value) for value in candidates):
            cell = selected_cells[cell_index]
            # 第 2 步：精确相交 + 面积判定。
            intersection = shapely.intersection(facility.geom.geometry, cell.geom.geometry,
                                                grid_size=tolerance.grid_size_m)
            area = _clip_area(intersection, tolerance)
            if area <= 0.0:
                degenerate_pairs += 1
                facility_stats[facility.id]["degenerate"] += 1
                near_misses.append({
                    "facility_id": facility.id,
                    "cell_id": cell.id,
                    "intersection_type": intersection.geom_type,
                    "intersection_area": float(shapely.area(intersection)),
                    "reason": "候选命中，但精确相交面积不超过判定容差（仅共边/近邻）",
                })
                continue
            canonical = _canonical(intersection, tolerance)
            if canonical is None or not shapely.is_valid(canonical):
                raise ArrangementError(f"设施 {facility.id} 与单元 {cell.id} 的相交结果不可用")
            exact_pairs += 1
            facility_stats[facility.id]["exact"] += 1
            pairs_by_cell.setdefault(cell.id, []).append(
                (facility.id, canonical, float(shapely.area(canonical))))

    cells = {cell_id: _arrange_cell(scene.cell_by_id[cell_id], pairs, tolerance)
             for cell_id, pairs in sorted(pairs_by_cell.items())}
    meta = {
        "method": "planar_polygonize",
        "facilities": list(_mask(*[facility.id for facility in facilities])),
        "selected_levels": {facility.id: facility.level for facility in facilities},
        "cells_touched": len(cells),
        "candidate_pairs": candidate_pairs,
        "exact_pairs": exact_pairs,
        "degenerate_pairs": degenerate_pairs,
        "tolerance": tolerance.to_dict(),
    }
    arrangement = Arrangement(
        scene=scene,
        tolerance=tolerance,
        facility_ids=_mask(*[facility.id for facility in facilities]),
        cells={cell_id: cells[cell_id] for cell_id in sorted(cells)},
        requested_ids=requested,
        missing_ids=tuple(sorted(missing)),
        candidate_pairs=candidate_pairs,
        exact_pairs=exact_pairs,
        degenerate_pairs=degenerate_pairs,
        arrangement=meta,
        cell_fingerprints={cell_id: scene.cell_by_id[cell_id].geom.fingerprint
                           for cell_id in sorted(cells)},
        facility_stats=facility_stats,
        near_misses=tuple(near_misses),
    )
    return arrangement, pairs_by_cell


def build_arrangements(scene: Scene,
                       selections: Sequence[Iterable[str]],
                       tolerance: Tolerance | None = None) -> list[Arrangement]:
    """一次枚举多个设施集合：对并集切一次，再逐个取子集。

    子集查询不重新枚举设施或重做平面排列；区别只体现在面片隶属集合。
    """
    selections = [_mask(*selection) for selection in selections]
    if any(not selection for selection in selections):
        raise UnknownFacilityError("查询未包含任何设施")
    union_ids = _mask(*[ident for selection in selections for ident in selection])
    base = build_arrangement(scene, union_ids, tolerance)
    return [base.subset(selection) for selection in selections]


def _arrange_cell(cell: CellSpec,
                  pairs: Sequence[tuple[str, Any, float]],
                  tolerance: Tolerance) -> CellArrangement:
    geometry = cell.geom.geometry
    cell_area = cell.geom.area
    clips = tuple(CellClip(facility_id=facility_id, cell_id=cell.id, area=area, geometry=clip)
                  for facility_id, clip, area in sorted(pairs, key=lambda item: item[0]))

    boundaries = [geometry.boundary] + [clip.geometry.boundary for clip in clips]
    merged = shapely.union_all(boundaries, grid_size=tolerance.grid_size_m)
    faces, cuts, dangles, invalid = polygonize_full(merged)
    if len(cuts.geoms) or len(dangles.geoms) or len(invalid.geoms):
        raise ArrangementError(
            f"单元 {cell.id} 的平面排列未闭合：cut={len(cuts.geoms)} "
            f"dangle={len(dangles.geoms)} invalid={len(invalid.geoms)}")

    clip_geometries = [clip.geometry for clip in clips]
    pieces: list[Piece] = []
    clipped_parts: list[float] = []
    for face in sorted(faces.geoms, key=lambda item: (item.area, item.bounds)):
        if face.area <= 0.0:
            continue
        if not geometry.covers(face):
            continue  # 单元孔洞或单元外的面片不属于任何人口单元
        point = face.representative_point()
        members = _mask(*[clip.facility_id for position, clip in enumerate(clips)
                          if clip_geometries[position].covers(point)])
        area = float(face.area)
        persons = cell.persons * area / cell_area
        pieces.append(Piece(cell_id=cell.id, index=len(pieces), area=area, persons=persons,
                            facility_ids=members, geometry=face))
        if members:
            clipped_parts.append(area)
    clipped_area = math.fsum(clipped_parts)

    covered_area = math.fsum(piece.area for piece in pieces)
    area_limit = max(tolerance.area(geometry), 1e-9 * max(1.0, cell_area))
    if abs(covered_area - cell_area) > area_limit:
        raise ArrangementError(
            f"单元 {cell.id} 的面片面积 {covered_area:.12g} 与单元面积 {cell_area:.12g} "
            f"不守恒（差 {covered_area - cell_area:.3g} m²）")

    multiplicities = [0.0] * (len(clips) + 1)
    for piece in pieces:
        multiplicities[len(piece.facility_ids)] += piece.persons
    total_persons = math.fsum(piece.persons for piece in pieces)
    if abs(total_persons - cell.persons) > 1e-9 * max(1.0, cell.persons):
        raise ArrangementError(
            f"单元 {cell.id} 的面片人数 {total_persons:.12g} 与登记人数 {cell.persons:.12g} 不守恒")

    return CellArrangement(cell=cell, clips=clips, pieces=tuple(pieces),
                           uncovered_persons=multiplicities[0],
                           multiplicity_persons=tuple(multiplicities),
                           total_persons=total_persons, clipped_area=clipped_area,
                           cell_area=cell_area)
