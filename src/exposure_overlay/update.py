"""增量更新：设施换版时只重算受影响的单元。

判定“受影响”的依据是新旧设施几何的对称差（用同一容差）。与对称差有实质面积
交叠的单元重切；其余单元沿用旧切片。人口单元几何或人数未变化才允许复用，
否则必须整体重算（``full_rebuild=True``）。
"""
from __future__ import annotations

from typing import Any, Iterable

import shapely

from .arrangement import Arrangement, build_arrangement, build_for_cells
from .core import Scene, Tolerance


def _empty_arrangement(scene: Scene, tolerance: Tolerance) -> Arrangement:
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


def changed_cells(old_scene: Scene, new_scene: Scene,
                  facility_ids: Iterable[str],
                  tolerance: Tolerance | None = None) -> dict[str, Any]:
    """比较同一设施在两版资料中的定义，列出受其变化影响的人口单元。"""
    tolerance = tolerance or Tolerance()
    rows: list[dict[str, Any]] = []
    affected: set[str] = set()
    for ident in sorted(set(facility_ids)):
        before = old_scene.facility_by_id.get(ident)
        after = new_scene.facility_by_id.get(ident)
        if before is None and after is None:
            continue
        if before is None or after is None:
            # 新设施可能覆盖任意单元：保守地把全部单元视为候选。
            touched = tuple(cell.id for cell in new_scene.cells)
            affected.update(touched)
            rows.append({
                "facility_id": ident,
                "change": "added" if before is None else "removed",
                "area_before": before.geom.area if before else 0.0,
                "area_after": after.geom.area if after else 0.0,
                "affected_cells": list(touched),
            })
            continue
        if (before.geom.fingerprint == after.geom.fingerprint
                and before.geom.area == after.geom.area
                and before.level == after.level):
            rows.append({
                "facility_id": ident,
                "change": "unchanged",
                "area_before": before.geom.area,
                "area_after": after.geom.area,
                "affected_cells": [],
            })
            continue
        delta = shapely.symmetric_difference(before.geom.geometry, after.geom.geometry,
                                             grid_size=tolerance.grid_size_m)
        delta_area = float(shapely.area(delta))
        threshold = tolerance.area(before.geom.geometry, after.geom.geometry, delta)
        cell_geometries = [cell.geom.geometry for cell in new_scene.cells]
        tree = shapely.STRtree(cell_geometries) if cell_geometries else None
        touched = []
        if tree is not None:
            for index in sorted(int(value) for value in tree.query(delta)):
                cell = new_scene.cells[index]
                overlap = float(shapely.area(shapely.intersection(
                    delta, cell.geom.geometry, grid_size=tolerance.grid_size_m)))
                if overlap > threshold:
                    touched.append(cell.id)
        affected.update(touched)
        rows.append({
            "facility_id": ident,
            "change": "geometry" if before.level == after.level else "geometry_and_level",
            "level_before": before.level,
            "level_after": after.level,
            "area_before": before.geom.area,
            "area_after": after.geom.area,
            "area_delta": after.geom.area - before.geom.area,
            "symmetric_difference_area": delta_area,
            "symmetric_difference": delta,
            "affected_cells": touched,
        })
    return {"facilities": rows, "affected_cells": sorted(affected)}


def _check_reusable(old_scene: Scene, new_scene: Scene) -> tuple[list[str], list[str]]:
    """确认人口资料可复用，返回 (共同单元, 新增单元)。"""
    old_ids = set(old_scene.cell_by_id)
    new_ids = set(new_scene.cell_by_id)
    if old_ids - new_ids:
        raise ValueError("人口单元在新版资料中消失，需要 full_rebuild=True：" +
                         ", ".join(sorted(old_ids - new_ids)))
    if old_scene.person_total != new_scene.person_total:
        raise ValueError("人口总量变化需要 full_rebuild=True")
    for cell_id in sorted(old_ids & new_ids):
        old = old_scene.cell_by_id[cell_id]
        new = new_scene.cell_by_id[cell_id]
        if old.persons != new.persons or old.geom.fingerprint != new.geom.fingerprint:
            raise ValueError("人口单元的几何或人数变化需要 full_rebuild=True：" + cell_id)
    return sorted(old_ids & new_ids), sorted(new_ids - old_ids)


def update_arrangement(old_scene: Scene,
                       old_arrangement: Arrangement,
                       new_scene: Scene,
                       facility_ids: Iterable[str],
                       tolerance: Tolerance | None = None,
                       full_rebuild: bool = False) -> tuple[Arrangement, dict[str, Any]]:
    """设施换版后的增量切片。

    - 未受几何变化影响的单元沿用旧面片与旧人口分配。
    - 受影响单元按新设施几何重切；若重切后不再被覆盖，则从切片中移除。
    - 人口资料必须不变（几何、人数、总量）；否则要求 ``full_rebuild=True``。
    """
    tolerance = tolerance or Tolerance()
    ids = tuple(sorted(set(facility_ids)))
    if new_scene.version != old_scene.version and not full_rebuild:
        raise ValueError("资料版本变化需要显式 full_rebuild=True；增量更新只适用于设施换版")
    reused_ids, new_cell_ids = _check_reusable(old_scene, new_scene)
    diff = changed_cells(old_scene, new_scene, ids, tolerance)
    # 已从新版资料移除的设施不能进入目标集合；新增设施按 ids 加入。
    target = tuple(sorted((set(old_arrangement.facility_ids) | set(ids))
                          & set(new_scene.facility_by_id)))
    if full_rebuild:
        rebuilt = build_arrangement(new_scene, target, tolerance)
        rebuilt.arrangement["incremental"] = False
        rebuilt.arrangement["facility_diff"] = diff["facilities"]
        return rebuilt, {**diff, "mode": "full_rebuild", "reused_cells": [],
                         "rebuilt_cells": sorted(rebuilt.cells)}
    if not target:
        # 所有相关设施都被移除：新排列为空，全部人口未覆盖。
        empty = _empty_arrangement(new_scene, tolerance)
        empty.arrangement.update({"incremental": True, "reused_cells": [],
                                  "rebuilt_cells": [], "changed_cells": [],
                                  "facility_diff": diff["facilities"]})
        return empty, {**diff, "mode": "incremental", "reused_cells": [],
                       "rebuilt_cells": []}

    affected = set(diff["affected_cells"]) | set(new_cell_ids)
    # 目标集合里新增（旧排列未切过）的设施，其覆盖单元也必须重切。
    added_to_slice = sorted(set(target) - set(old_arrangement.facility_ids))
    for ident in added_to_slice:
        facility = new_scene.facility_by_id.get(ident)
        if facility is None:
            continue
        cell_geometries = [cell.geom.geometry for cell in new_scene.cells]
        if not cell_geometries:
            continue
        tree = shapely.STRtree(cell_geometries)
        for index in sorted(int(value) for value in tree.query(facility.geom.geometry,
                                                               predicate="intersects")):
            overlap = shapely.area(shapely.intersection(
                facility.geom.geometry, new_scene.cells[index].geom.geometry,
                grid_size=tolerance.grid_size_m))
            if float(overlap) > tolerance.area(facility.geom.geometry):
                affected.add(new_scene.cells[index].id)
    fresh = build_for_cells(new_scene, target, sorted(affected), tolerance)
    cells = dict(fresh.cells)
    reused_cells: list[str] = []
    for cell_id in reused_ids:
        if cell_id in affected or cell_id in cells:
            continue
        old_cell = old_arrangement.cells.get(cell_id)
        if old_cell is None:
            continue  # 旧版中本来就不受覆盖
        # 复用的旧面片只记录了旧排列的设施集合。若新旧排列的设施集合相同，
        # 可原样复用；集合不同时，旧面片的隶属集合可能缺项，必须重切。
        if set(old_arrangement.facility_ids) != set(target):
            continue
        cells[cell_id] = old_cell
        reused_cells.append(cell_id)

    rebuilt_cells = sorted(fresh.cells)
    arrangement = Arrangement(
        scene=new_scene,
        tolerance=tolerance,
        facility_ids=target,
        cells={cell_id: cells[cell_id] for cell_id in sorted(cells)},
        requested_ids=fresh.requested_ids,
        missing_ids=fresh.missing_ids,
        candidate_pairs=fresh.candidate_pairs,
        exact_pairs=fresh.exact_pairs,
        degenerate_pairs=fresh.degenerate_pairs,
        arrangement=dict(fresh.arrangement),
        cell_fingerprints={cell_id: new_scene.cell_by_id[cell_id].geom.fingerprint
                           for cell_id in sorted(cells)},
        facility_stats=dict(fresh.facility_stats),
        near_misses=fresh.near_misses,
    )
    arrangement.arrangement.update({
        "incremental": True,
        "reused_cells": reused_cells,
        "rebuilt_cells": rebuilt_cells,
        "changed_cells": sorted(affected),
        "facility_diff": diff["facilities"],
        "statistics_scope": "候选/精确对数只统计本次重切单元；复用单元沿用旧统计",
    })
    report = {**{key: value for key, value in diff.items() if key != "facilities"},
              "mode": "incremental", "reused_cells": reused_cells,
              "rebuilt_cells": rebuilt_cells,
              "facility_diff": [
                  {key: value for key, value in row.items() if key != "symmetric_difference"}
                  for row in diff["facilities"]
              ]}
    return arrangement, report
