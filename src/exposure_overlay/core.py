"""资料校验、几何归一化与修复辅助。

所有面积统计均假定输入使用米制等面积平面坐标。几何判定的容差由 ``Tolerance``
统一给出；容差只参与“是否相交/是否重合”的判定，从不用于逐片人数取整。
"""
from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import shapely
from shapely.geometry import MultiPolygon, Polygon, shape

# GeoJSON 中允许的几何类型：影响区允许带孔洞的 Polygon/MultiPolygon。
GEOMETRY_TYPES = ("Polygon", "MultiPolygon")
COLLECTION = "population"


class ExposureError(ValueError):
    """本组件的基类异常。"""


class SchemaError(ExposureError):
    """资料结构不符合约定。"""


class InvalidGeometryError(ExposureError):
    """几何无效，且当前调用没有显式选择修复方案。"""


class InvalidInputError(ExposureError):
    """资料之间关系非法（例如人口单元实质重叠）。"""


class UnknownFacilityError(ExposureError):
    """查询引用了资料中不存在的设施。"""


class ArrangementError(ExposureError):
    """切片排列自检失败；说明内部不变量被破坏，不能返回可疑数字。"""


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class Tolerance:
    """几何判定容差。

    ``grid_size_m`` 是坐标吸附网格（米）。面积容差取网格面积与
    ``0.5 * grid * 周长`` 的较大者，用于吸收吸附造成的边界抖动。
    它只用于判断“是否有实质交叠/覆盖”，不参与人数分配。
    """

    grid_size_m: float = 1e-6

    def __post_init__(self) -> None:
        if not _finite_number(self.grid_size_m) or self.grid_size_m <= 0:
            raise ValueError("grid_size_m 必须为正的有限数")

    @property
    def grid_area(self) -> float:
        return self.grid_size_m * self.grid_size_m

    def area(self, *geometries: Any) -> float:
        """返回这组几何的面积判定容差（平方米）。"""
        perimeter = 0.0
        for geometry in geometries:
            if geometry is None:
                continue
            perimeter += float(shapely.length(geometry))
        if perimeter <= 0:
            return self.grid_area
        return max(self.grid_area, 0.5 * self.grid_size_m * perimeter)

    def to_dict(self) -> dict[str, Any]:
        return {"grid_size_m": self.grid_size_m,
                "grid_area_m2": self.grid_area,
                "note": "面积容差= max(网格面积, 0.5*grid*周长)；仅用于几何判定"}


@dataclass(frozen=True)
class NormalizedGeometry:
    """吸附到统一网格、且已按语义合并多部件的几何。"""

    geometry: Polygon | MultiPolygon
    area: float
    source_area: float
    source_type: str
    fingerprint: str
    vertices: int
    parts: int
    holes: int
    dissolved_parts: int
    area_delta: float

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.geometry.geom_type,
                "area": self.area,
                "source_area": self.source_area,
                "source_type": self.source_type,
                "fingerprint": self.fingerprint,
                "vertices": self.vertices,
                "parts": self.parts,
                "holes": self.holes,
                "dissolved_parts": self.dissolved_parts,
                "area_delta": self.area_delta}


@dataclass(frozen=True)
class CellSpec:
    id: str
    persons: float
    geom: NormalizedGeometry


@dataclass(frozen=True)
class FacilitySpec:
    id: str
    level: int
    geom: NormalizedGeometry


@dataclass(frozen=True)
class Scene:
    version: str
    cells: tuple[CellSpec, ...]
    facilities: tuple[FacilitySpec, ...]
    cell_by_id: Mapping[str, CellSpec]
    facility_by_id: Mapping[str, FacilitySpec]
    person_total: float


# ---------------------------------------------------------------------------
# 结构检查


def check_bundle(bundle: Mapping[str, Any]) -> None:
    """检查资料的外层结构与业务前提，不做几何推断。"""
    if not isinstance(bundle, Mapping):
        raise SchemaError("资料根节点必须为对象")
    if type(bundle.get("schema_version")) is not int or bundle["schema_version"] != 1:
        raise SchemaError("不支持的资料结构版本")
    for name in ("schema_version", COLLECTION, "facilities", "crs", "version"):
        if name not in bundle:
            raise SchemaError("缺少资料字段：" + name)
    if not isinstance(bundle[COLLECTION], list) or not bundle[COLLECTION]:
        raise SchemaError("主资料集合必须为非空列表：" + COLLECTION)
    if not isinstance(bundle["facilities"], list):
        raise SchemaError("设施集合必须为列表")
    version = bundle["version"]
    if not isinstance(version, str) or not version:
        raise SchemaError("version 必须为非空字符串")
    crs = bundle["crs"]
    if not isinstance(crs, Mapping):
        raise SchemaError("crs 必须为对象")
    if crs.get("kind") != "equal_area_planar" or crs.get("unit") != "m":
        raise SchemaError("仅支持等面积平面米制坐标（crs.kind=equal_area_planar, crs.unit=m）")


def _check_positions(ring: Any, where: str) -> None:
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        raise InvalidGeometryError(where + "：环至少需要 3 个位置")
    for position in ring:
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise InvalidGeometryError(where + "：坐标必须为二维 [x, y]")
        if not all(_finite_number(value) for value in position):
            raise InvalidGeometryError(where + "：坐标含非有限数或非数字")


def _check_coordinates(coords: Any, geometry_type: str, where: str) -> None:
    if not isinstance(coords, (list, tuple)) or not coords:
        raise InvalidGeometryError(where + "：coordinates 不能为空")
    if geometry_type == "Polygon":
        for ring in coords:
            _check_positions(ring, where)
        return
    for polygon in coords:
        if not isinstance(polygon, (list, tuple)) or not polygon:
            raise InvalidGeometryError(where + "：MultiPolygon 的每个部件必须为非空环列表")
        for ring in polygon:
            _check_positions(ring, where)


def parse_geometry(raw: Any, role: str, ident: str) -> Polygon | MultiPolygon:
    """把 GeoJSON 几何转为 Shapely 对象，并拒绝不支持的形状。"""
    where = f"{role}:{ident}"
    if not isinstance(raw, Mapping):
        raise InvalidGeometryError(where + "：geometry 必须为对象")
    geometry_type = raw.get("type")
    if geometry_type not in GEOMETRY_TYPES:
        raise InvalidGeometryError(where + "：仅支持 Polygon 或 MultiPolygon，得到 " + repr(geometry_type))
    _check_coordinates(raw.get("coordinates"), geometry_type, where)
    try:
        geometry = shape(raw)
    except Exception as exc:  # noqa: BLE001 - 统一包装为资料错误
        raise InvalidGeometryError(where + "：无法构造几何：" + str(exc)) from exc
    if geometry.is_empty:
        raise InvalidGeometryError(where + "：几何为空")
    if shapely.has_z(geometry):
        raise InvalidGeometryError(where + "：几何含高程/第三维")
    return geometry


def polygonal_parts(geometry: Any) -> list[Polygon]:
    """递归提取几何中的面部件，忽略点/线。"""
    if geometry is None or geometry.is_empty:
        return []
    geometry_type = geometry.geom_type
    if geometry_type == "Polygon":
        return [geometry]
    if geometry_type == "MultiPolygon":
        return list(geometry.geoms)
    if geometry_type == "GeometryCollection":
        parts: list[Polygon] = []
        for item in geometry.geoms:
            parts.extend(polygonal_parts(item))
        return parts
    return []


def _canonical_polygonal(geometry: Any, tolerance: Tolerance) -> Polygon | MultiPolygon | None:
    """把任意结果收敛为有效面几何；返回 None 表示没有面。"""
    parts = polygonal_parts(geometry)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = shapely.union_all(parts, grid_size=tolerance.grid_size_m)
    if merged.is_empty:
        return None
    if merged.geom_type in GEOMETRY_TYPES:
        return merged
    parts = polygonal_parts(merged)
    if not parts:
        return None
    return shapely.union_all(parts, grid_size=tolerance.grid_size_m)


def _fingerprint(geometry: Any) -> str:
    return hashlib.sha256(shapely.to_wkb(geometry, byte_order=1)).hexdigest()


def _topology(geometry: Polygon | MultiPolygon) -> tuple[int, int, int]:
    parts = polygonal_parts(geometry)
    holes = sum(len(part.interiors) for part in parts)
    vertices = sum(len(part.exterior.coords) + sum(len(ring.coords) for ring in part.interiors)
                   for part in parts)
    return len(parts), holes, vertices


def normalize_geometry(raw: Any, role: str, ident: str, tolerance: Tolerance) -> NormalizedGeometry:
    """解析、检查、合并自重叠并吸附到网格。

    - 设施的多部件影响面按“自身重叠先取并”处理。
    - 人口单元的多部件若实质重叠则视为非法输入。
    - 单部件几何若本身无效（自交、孔洞穿壳等）一律报错，等待独立修复方案。
    """
    geometry = parse_geometry(raw, role, ident)
    source_area = float(shapely.area(geometry))
    parts = polygonal_parts(geometry)
    if not parts:
        raise InvalidGeometryError(f"{role}:{ident}：没有面部件")
    for index, part in enumerate(parts):
        if part.is_empty or not shapely.is_valid(part):
            reason = shapely.is_valid_reason(part)
            raise InvalidGeometryError(f"{role}:{ident}：第{index + 1}个面部件无效（{reason}）")

    overlap_limit = tolerance.area(*parts) if len(parts) > 1 else 0.0
    if role == "population" and len(parts) > 1:
        for left in range(len(parts)):
            for right in range(left + 1, len(parts)):
                overlap = _clean_area(shapely.intersection(parts[left], parts[right],
                                                           grid_size=tolerance.grid_size_m),
                                      tolerance)
                if overlap > overlap_limit:
                    raise InvalidInputError(
                        f"{role}:{ident}：多部件实质重叠（{overlap:.12g} m²），人口单元不可重复计数")

    dissolved_parts = len(parts)
    if len(parts) == 1:
        dissolved = parts[0]
    else:
        dissolved = shapely.union_all(parts, grid_size=tolerance.grid_size_m)
        if dissolved.is_empty:
            raise InvalidGeometryError(f"{role}:{ident}：多部件合并后为空")

    snapped = shapely.set_precision(dissolved, tolerance.grid_size_m, mode="valid_output")
    snapped = _canonical_polygonal(snapped, tolerance)
    if snapped is None or snapped.is_empty:
        raise InvalidGeometryError(f"{role}:{ident}：吸附到 {tolerance.grid_size_m:g} m 网格后消失")
    if not shapely.is_valid(snapped):
        raise InvalidGeometryError(
            f"{role}:{ident}：吸附后几何无效（{shapely.is_valid_reason(snapped)}）")
    area = float(shapely.area(snapped))
    if area <= tolerance.grid_area:
        raise InvalidGeometryError(f"{role}:{ident}：面积 {area:.12g} m² 不大于网格面积，视为退化")
    parts_count, holes, vertices = _topology(snapped)
    return NormalizedGeometry(
        geometry=snapped,
        area=area,
        source_area=source_area,
        source_type=raw.get("type"),
        fingerprint=_fingerprint(snapped),
        vertices=vertices,
        parts=parts_count,
        holes=holes,
        dissolved_parts=dissolved_parts,
        area_delta=area - source_area,
    )


def _clean_area(geometry: Any, tolerance: Tolerance) -> float:
    """几何交叠面积；只有面积大于判定容差才算“拥有面积”。"""
    canonical = _canonical_polygonal(geometry, tolerance)
    if canonical is None:
        return 0.0
    area = float(shapely.area(canonical))
    return area if area > tolerance.area(canonical) else 0.0


def parse_scene(bundle: Mapping[str, Any], tolerance: Tolerance | None = None) -> Scene:
    """把资料字典转换为可计算的场景，并在入口完成全部合法性检查。"""
    tolerance = tolerance or Tolerance()
    check_bundle(bundle)
    cells: list[CellSpec] = []
    seen_cells: set[str] = set()
    for index, record in enumerate(bundle[COLLECTION]):
        if not isinstance(record, Mapping):
            raise SchemaError(f"population[{index}] 必须为对象")
        ident = record.get("id")
        if not isinstance(ident, str) or not ident:
            raise SchemaError(f"population[{index}] 缺少非空字符串 id")
        if ident in seen_cells:
            raise SchemaError("人口单元 id 重复：" + ident)
        seen_cells.add(ident)
        persons = record.get("persons")
        if not _finite_number(persons) or persons < 0:
            raise SchemaError(f"population:{ident} 的 persons 必须为非负有限数")
        if "geometry" not in record:
            raise SchemaError(f"population:{ident} 缺少 geometry")
        normalized = normalize_geometry(record["geometry"], "population", ident, tolerance)
        cells.append(CellSpec(id=ident, persons=float(persons), geom=normalized))

    facilities: list[FacilitySpec] = []
    seen_facilities: set[str] = set()
    for index, record in enumerate(bundle["facilities"]):
        if not isinstance(record, Mapping):
            raise SchemaError(f"facilities[{index}] 必须为对象")
        ident = record.get("id")
        if not isinstance(ident, str) or not ident:
            raise SchemaError(f"facilities[{index}] 缺少非空字符串 id")
        if ident in seen_facilities:
            raise SchemaError("设施 id 重复：" + ident)
        seen_facilities.add(ident)
        level = record.get("level", 0)
        if type(level) is not int:
            raise SchemaError(f"facility:{ident} 的 level 必须为整数")
        if "geometry" not in record:
            raise SchemaError(f"facility:{ident} 缺少 geometry")
        normalized = normalize_geometry(record["geometry"], "facility", ident, tolerance)
        facilities.append(FacilitySpec(id=ident, level=level, geom=normalized))

    cells.sort(key=lambda item: item.id)
    facilities.sort(key=lambda item: item.id)
    _check_cell_overlaps(cells, tolerance)
    return Scene(
        version=bundle["version"],
        cells=tuple(cells),
        facilities=tuple(facilities),
        cell_by_id={cell.id: cell for cell in cells},
        facility_by_id={facility.id: facility for facility in facilities},
        person_total=float(sum(cell.persons for cell in cells)),
    )


def _check_cell_overlaps(cells: Sequence[CellSpec], tolerance: Tolerance) -> None:
    """人口单元之间不允许实质重叠；边界接触允许。"""
    if len(cells) < 2:
        return
    geometries = [cell.geom.geometry for cell in cells]
    tree = shapely.STRtree(geometries)
    problems: list[str] = []
    for left in range(len(cells)):
        candidates = tree.query(geometries[left])
        for right in candidates:
            if right <= left:
                continue
            intersection = shapely.intersection(geometries[left], geometries[right],
                                                grid_size=tolerance.grid_size_m)
            area = _clean_area(intersection, tolerance)
            limit = tolerance.area(geometries[left], geometries[right])
            if area > limit:
                problems.append(f"{cells[left].id} 与 {cells[right].id} 重叠 {area:.12g} m²")
    if problems:
        raise InvalidInputError("人口单元实质重叠：" + "；".join(problems[:10]))


# ---------------------------------------------------------------------------
# 修复方案（显式、独立，不在正式查询中隐式使用）


def _geojson(geometry: Any) -> dict[str, Any]:
    from shapely.geometry import mapping

    return mapping(geometry)


def repair_bundle(bundle: Mapping[str, Any],
                  tolerance: Tolerance | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成独立的修复资料与报告，记录每个几何的面积和拓扑变化。

    修复只用 Shapely/GEOS 的 ``make_valid``，随后按设施/人口规则合并并吸附。
    报告同时给出修复后资料是否可直接用于正式查询（跨单元重叠等仍需源数据修正）。
    """
    tolerance = tolerance or Tolerance()
    check_bundle(bundle)
    repaired = copy.deepcopy(dict(bundle))
    rows: list[dict[str, Any]] = []

    def repair_one(record: Mapping[str, Any], role: str) -> Mapping[str, Any]:
        ident = record.get("id")
        raw = record.get("geometry")
        geometry = parse_geometry(raw, role, str(ident))
        before_parts, before_holes, before_vertices = _topology(geometry)
        valid_before = bool(shapely.is_valid(geometry))
        row = {
            "role": role,
            "id": ident,
            "valid_before": valid_before,
            "reason": None if valid_before else shapely.is_valid_reason(geometry),
            "area_before": float(shapely.area(geometry)),
            "parts_before": before_parts,
            "holes_before": before_holes,
            "vertices_before": before_vertices,
        }
        if valid_before:
            row.update({"changed": False, "valid_after": True,
                        "area_after": float(shapely.area(geometry)),
                        "parts_after": before_parts, "holes_after": before_holes,
                        "vertices_after": before_vertices})
            rows.append(row)
            return record

        fixed = shapely.make_valid(geometry)
        fixed = _canonical_polygonal(fixed, tolerance)
        if fixed is None or fixed.is_empty:
            row.update({"changed": False, "valid_after": False, "error": "修复后没有面部件"})
            rows.append(row)
            raise InvalidGeometryError(
                f"{role}:{ident} 无法修复为面几何（原因：{row['reason']}）")
        snapped = shapely.set_precision(fixed, tolerance.grid_size_m, mode="valid_output")
        snapped = _canonical_polygonal(snapped, tolerance)
        if snapped is None or snapped.is_empty or not shapely.is_valid(snapped):
            row.update({"changed": False, "valid_after": False,
                        "error": "修复结果吸附后无效或为空"})
            rows.append(row)
            raise InvalidGeometryError(f"{role}:{ident} 的修复结果不可用")
        after_parts, after_holes, after_vertices = _topology(snapped)
        row.update({
            "changed": True,
            "valid_after": True,
            "area_after": float(shapely.area(snapped)),
            "parts_after": after_parts,
            "holes_after": after_holes,
            "vertices_after": after_vertices,
            "topology_changed": (before_parts, before_holes, before_vertices)
                                != (after_parts, after_holes, after_vertices),
        })
        rows.append(row)
        return {**record, "geometry": _geojson(snapped)}

    repaired[COLLECTION] = [repair_one(record, "population") for record in bundle[COLLECTION]]
    repaired["facilities"] = [repair_one(record, "facility") for record in bundle["facilities"]]

    report: dict[str, Any] = {
        "version": bundle["version"],
        "tolerance": tolerance.to_dict(),
        "geometries": rows,
        "changed_count": sum(1 for row in rows if row.get("changed")),
        "query_ready": True,
        "query_error": None,
    }
    try:
        parse_scene(repaired, tolerance)
    except ExposureError as exc:
        report["query_ready"] = False
        report["query_error"] = f"{type(exc).__name__}: {exc}"
    return repaired, report


def load_scene(path: str | Any, tolerance: Tolerance | None = None) -> Scene:
    """从资料文件读取场景；沿用现有读取模块的结构检查。"""
    from ..exposure_overlay_input import load_bundle

    bundle = load_bundle(path)
    return parse_scene(bundle, tolerance)


__all__ = [
    "ArrangementError",
    "CellSpec",
    "ExposureError",
    "FacilitySpec",
    "InvalidGeometryError",
    "InvalidInputError",
    "NormalizedGeometry",
    "Scene",
    "SchemaError",
    "Tolerance",
    "UnknownFacilityError",
    "check_bundle",
    "load_scene",
    "normalize_geometry",
    "parse_geometry",
    "parse_scene",
    "polygonal_parts",
    "repair_bundle",
]
