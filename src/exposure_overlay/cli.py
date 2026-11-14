"""人口暴露统计的命令行入口。

示例::

    python -m src.exposure_overlay.cli query fixtures/sample.json --facilities west,east --thresholds 1,2
    python -m src.exposure_overlay.cli query fixtures/sample.json --level-min 1
    python -m src.exposure_overlay.cli ledger fixtures/sample.json --facilities west,east
    python -m src.exposure_overlay.cli repair fixtures/invalid.json
    python -m src.exposure_overlay.cli compare fixtures/sample.json fixtures/sample_shifted.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .service import ExposureService


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _thresholds(value: str | None) -> list[int]:
    if not value:
        return [1]
    return [int(part) for part in value.split(",") if part.strip()]


def _load(path: str) -> ExposureService:
    return ExposureService.load(path)


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, default=str))


def cmd_query(args: argparse.Namespace) -> int:
    service = _load(args.data)
    coverage = service.query(_csv(args.facilities) or None, args.level_min,
                             _thresholds(args.thresholds))
    output = coverage.to_dict()
    output["ledger_totals"] = service.ledger_totals(coverage)
    if args.ledger:
        output["ledger"] = service.ledger(coverage, include_geometry=args.geometry)
    _emit(output)
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    service = _load(args.data)
    coverage = service.query(_csv(args.facilities) or None, args.level_min,
                             _thresholds(args.thresholds))
    rows = service.ledger(coverage, include_geometry=args.geometry)
    _emit({"version": coverage.version,
           "facility_ids": list(coverage.facility_ids),
           "thresholds": list(coverage.thresholds),
           "totals": service.ledger_totals(coverage),
           "coverage": coverage.to_dict(),
           "ledger": rows})
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    from .core import repair_bundle
    from ..exposure_overlay_input import load_bundle

    bundle = load_bundle(args.data)
    _, report = repair_bundle(bundle)
    _emit(report)
    return 0 if report["changed_count"] or report["query_ready"] else 1


def cmd_compare(args: argparse.Namespace) -> int:
    old = _load(args.old)
    new = _load(args.new)
    ids = _csv(args.facilities) or None
    thresholds = _thresholds(args.thresholds)
    result = old.compare(new, ids, args.level_min, thresholds)
    if args.ledger:
        result["new_ledger"] = new.ledger(new.query(ids, args.level_min, thresholds),
                                          include_geometry=args.geometry)
    _emit(result)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from .render import render_svg

    service = _load(args.data)
    ids = _csv(args.facilities) or None
    coverage = service.query(ids, args.level_min, _thresholds(args.thresholds))
    arrangement = service.arrangement(coverage.facility_ids)
    svg = render_svg(arrangement, coverage)
    args.output.write_text(svg, encoding="utf-8")
    print(str(args.output))
    return 0


def cmd_example(args: argparse.Namespace) -> int:
    """内置示例：100 人正方形 + 两块各 60、交叠 20 的矩形。"""
    from .examples import build_example_bundle

    service = ExposureService.from_bundle(build_example_bundle())
    coverage = service.query(["west", "east"], thresholds=[1, 2])
    output = coverage.to_dict()
    output["ledger"] = service.ledger(coverage)
    _emit(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人口暴露统计")
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="查询并集/交集/恰好 k 人")
    query.add_argument("data", type=Path)
    query.add_argument("--facilities", help="逗号分隔的设施 id")
    query.add_argument("--level-min", type=int, default=None)
    query.add_argument("--thresholds", default="1,2")
    query.add_argument("--ledger", action="store_true", help="输出面片台账")
    query.add_argument("--geometry", action="store_true", help="台账附带面片 GeoJSON")
    query.set_defaults(func=cmd_query)

    ledger = sub.add_parser("ledger", help="输出面片台账与复核总量")
    ledger.add_argument("data", type=Path)
    ledger.add_argument("--facilities")
    ledger.add_argument("--level-min", type=int, default=None)
    ledger.add_argument("--thresholds", default="1,2")
    ledger.add_argument("--geometry", action="store_true", help="台账附带面片 GeoJSON")
    ledger.set_defaults(func=cmd_ledger)

    repair = sub.add_parser("repair", help="修复无效几何并输出独立报告")
    repair.add_argument("data", type=Path)
    repair.set_defaults(func=cmd_repair)

    compare = sub.add_parser("compare", help="比较两版资料的同一查询")
    compare.add_argument("old", type=Path)
    compare.add_argument("new", type=Path)
    compare.add_argument("--facilities")
    compare.add_argument("--level-min", type=int, default=None)
    compare.add_argument("--thresholds", default="1,2")
    compare.add_argument("--ledger", action="store_true")
    compare.add_argument("--geometry", action="store_true")
    compare.set_defaults(func=cmd_compare)

    render = sub.add_parser("render", help="输出 SVG 复核视图")
    render.add_argument("data", type=Path)
    render.add_argument("output", type=Path)
    render.add_argument("--facilities")
    render.add_argument("--level-min", type=int, default=None)
    render.add_argument("--thresholds", default="1,2,3")
    render.set_defaults(func=cmd_render)

    example = sub.add_parser("example", help="运行内置 100 人示例")
    example.set_defaults(func=cmd_example)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
