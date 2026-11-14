"""把切片结果画成 SVG，便于人工复核面片归属。

不是统计路径的一部分；只读 ``Arrangement`` / ``Coverage``。
"""
from __future__ import annotations

from typing import Any

import shapely

PALETTE = [
    "#e8f0fe", "#fce8e6", "#e6f4ea", "#fef7e0", "#f3e8fd", "#e0f7fa",
]

FACILITY_COLORS = {
    0: "#d93025",   # k=0
    1: "#1a73e8",   # exactly 1
    2: "#188038",   # exactly 2
    3: "#e37400",   # exactly 3
}


def _rings(geometry: Any) -> str:
    parts = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    path = []
    for part in parts:
        for ring in [part.exterior, *part.interiors]:
            coords = list(ring.coords)
            if not coords:
                continue
            commands = [f"M {coords[0][0]:.6f} {coords[0][1]:.6f}"]
            commands.extend(f"L {x:.6f} {y:.6f}" for x, y in coords[1:])
            commands.append("Z")
            path.append(" ".join(commands))
    return " ".join(path)


def render_svg(arrangement: Any, coverage: Any, width: int = 640, height: int = 640,
               scale: float = 40.0, margin: float = 24.0) -> str:
    """绘制人口单元、面片与设施轮廓；颜色按查询下的重数分类。"""
    pieces = arrangement.pieces
    if not pieces:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    bounds = shapely.total_bounds([piece.geometry for piece in pieces]
                                  + [cell.cell.geom.geometry for cell in arrangement.cells.values()])
    minx, miny, maxx, maxy = bounds
    canvas_w = (maxx - minx) * scale + 2 * margin
    canvas_h = (maxy - miny) * scale + 2 * margin

    def project(x: float, y: float) -> tuple[float, float]:
        return (margin + (x - minx) * scale, margin + (maxy - y) * scale)

    def transform(geometry: Any) -> str:
        # 用仿射变换后直接输出路径，避免逐点改写字符串。
        from shapely.affinity import affine_transform

        mapped = affine_transform(geometry, [scale, 0, 0, -scale,
                                             margin - minx * scale,
                                             margin + maxy * scale])
        return _rings(mapped)

    output = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w:.1f}" '
              f'height="{canvas_h:.1f}" viewBox="0 0 {canvas_w:.1f} {canvas_h:.1f}">',
              '<rect width="100%" height="100%" fill="#ffffff"/>']
    for cell_id in sorted(arrangement.cells):
        cell = arrangement.cells[cell_id]
        output.append(f'<path d="{transform(cell.cell.geom.geometry)}" fill="#f8f9fa" '
                      f'stroke="#202124" stroke-width="1.5" fill-rule="evenodd"/>')
    for piece in pieces:
        counts = coverage.piece_counts(piece)
        color = FACILITY_COLORS.get(min(counts["k"], 3), "#5f6368")
        opacity = 0.75 if counts["in_union"] else 0.25
        title = (f"{piece.cell_id} #{piece.index}\\n"
                 f"k={counts['k']} area={piece.area:.6f} persons={piece.persons:.6f}\\n"
                 f"facilities={','.join(piece.facility_ids) or '-'}")
        output.append(f'<path d="{transform(piece.geometry)}" fill="{color}" '
                      f'fill-opacity="{opacity}" stroke="#5f6368" stroke-width="0.7" '
                      f'fill-rule="evenodd"><title>{title}</title></path>')
    output.append("</svg>")
    return "\n".join(output)
