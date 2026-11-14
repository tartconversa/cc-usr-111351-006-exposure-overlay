"""人口暴露统计组件的回归检查。"""
import json
import math
import unittest
from pathlib import Path

import shapely

from src.exposure_overlay import (
    ExposureService,
    InvalidGeometryError,
    InvalidInputError,
    QuerySpec,
    SchemaError,
    UnknownFacilityError,
    compare_versions,
    ledger_totals,
    repair_bundle,
)
from src.exposure_overlay.examples import (
    build_containment_bundle,
    build_example_bundle,
    build_hole_bundle,
    build_multipart_bundle,
    build_nested_bundle,
    build_shared_edge_bundle,
    build_shifted_bundle,
    build_two_cell_bundle,
)
from src.exposure_overlay.update import changed_cells, update_arrangement

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.json"


def service(bundle):
    return ExposureService.from_bundle(bundle)


class CanonicalExampleTests(unittest.TestCase):
    """需求中的基准例子：并集 100、恰好一处 80、恰好两处 20。"""

    def setUp(self):
        self.service = service(build_example_bundle())
        self.coverage = self.service.query(["west", "east"], thresholds=[1, 2])

    def test_headline_numbers(self):
        self.assertEqual(self.coverage.union, 100)
        self.assertEqual(self.coverage.exact, {0: 0.0, 1: 80.0, 2: 20.0})
        self.assertEqual(self.coverage.intersection, 20)
        self.assertEqual(self.coverage.uncovered, 0)

    def test_at_least(self):
        self.assertEqual(self.coverage.at_least, {1: 100, 2: 20})

    def test_pieces_reproduce_result(self):
        rows = self.service.ledger(self.coverage)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row["area"] for row in rows), [20, 40, 40])
        self.assertEqual(sorted(row["persons"] for row in rows), [20, 40, 40])
        totals = ledger_totals(rows)
        self.assertEqual(totals["persons"], 100)
        self.assertEqual(totals["union"], 100)
        self.assertEqual(totals["intersection"], 20)
        self.assertEqual(totals["exact_k"], {"1": 80.0, "2": 20.0})

    def test_ledger_totals_match_coverage(self):
        rows = self.service.ledger(self.coverage)
        totals = ledger_totals(rows)
        self.assertEqual(totals["union"], self.coverage.union)
        self.assertEqual(totals["intersection"], self.coverage.intersection)
        self.assertEqual(totals["uncovered"], self.coverage.uncovered)

    def test_conservation(self):
        conservation = self.coverage.conservation
        self.assertTrue(conservation["conservation_within_tolerance"])
        self.assertTrue(conservation["area_conservation_within_tolerance"])
        self.assertAlmostEqual(conservation["sum_exact_k"], 100.0)
        self.assertAlmostEqual(conservation["uncovered_plus_covered"], 100.0)

    def test_geometry_of_each_piece(self):
        # 三块面片必须是西侧 40、中间 20、东侧 40，且中间同时隶属两设施。
        rows = self.service.ledger(self.coverage)
        overlap = [row for row in rows if row["k"] == 2]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["area"], 20)
        self.assertEqual(overlap[0]["facility_ids"], ["east", "west"])

    def test_ledger_geometry_reproduces_piece_area(self):
        from shapely.geometry import shape

        rows = self.service.ledger(self.coverage, include_geometry=True)
        for row in rows:
            rebuilt = shape(row["geometry"])
            self.assertAlmostEqual(rebuilt.area, row["area"], places=9)
            self.assertAlmostEqual(rebuilt.area * row["persons_per_m2"],
                                   row["persons"], places=9)
        # 三个面片几何互不重叠，且并起来正好是整个单元。
        union = shapely.union_all([shape(row["geometry"]) for row in rows])
        self.assertAlmostEqual(union.area, 100.0, places=9)


class CoverSemanticsTests(unittest.TestCase):
    def test_hole_is_not_covered(self):
        coverage = service(build_hole_bundle()).query(["ringed"])
        self.assertEqual(coverage.union, 84)
        self.assertEqual(coverage.uncovered, 16)
        self.assertEqual(coverage.exact[1], 84)
        self.assertEqual(coverage.conservation["conservation_error"], 0.0)

    def test_multipart_facility_and_population(self):
        coverage = service(build_multipart_bundle()).query(["twin"])
        self.assertEqual(coverage.union, 64)
        self.assertEqual(coverage.uncovered, 16)
        rows = ledger_totals(service(build_multipart_bundle()).ledger(coverage))
        self.assertEqual(rows["persons"], 80)

    def test_fully_contained(self):
        coverage = service(build_containment_bundle()).query(["outer", "overlay"],
                                                             thresholds=[1, 2])
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.exact[1], 60)
        self.assertEqual(coverage.exact[2], 40)
        self.assertEqual(coverage.intersection, 40)

    def test_shared_edge_is_zero_area(self):
        svc = service(build_shared_edge_bundle())
        coverage = svc.query(["left", "right"], thresholds=[1, 2])
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.intersection, 0)
        self.assertEqual(coverage.exact[2], 0)
        # 共边在近邻索引中是候选，但精确相交面积为 0，必须落在 near_misses。
        self.assertEqual(coverage.spatial["candidate_pairs"], 2)
        self.assertEqual(coverage.spatial["exact_pairs"], 2)
        self.assertEqual(coverage.spatial["degenerate_pairs"], 0)

    def test_near_neighbor_is_not_intersection(self):
        bundle = build_example_bundle()
        bundle["facilities"] = [{"id": "near", "level": 1,
                                 "geometry": {"type": "Polygon",
                                              "coordinates": [[[10.2, 0], [14, 0],
                                                               [14, 10], [10.2, 10],
                                                               [10.2, 0]]]}}]
        svc = service(bundle)
        coverage = svc.query(["near"])
        self.assertEqual(coverage.union, 0)
        self.assertEqual(coverage.uncovered, 100)
        self.assertEqual(coverage.spatial["candidate_pairs"], 0)
        # 若 bbox 与单元相交但精确面积为 0，必须记入 near_misses 而不是人口。
        self.assertEqual(coverage.exact[1], 0)

    def test_tolerance_does_not_round_people(self):
        bundle = build_example_bundle()
        bundle["facilities"][1]["geometry"]["coordinates"] = [
            [[3.999999, 0], [10, 0], [10, 10], [3.999999, 10], [3.999999, 0]]]
        coverage = service(bundle).query(["west", "east"], thresholds=[1, 2])
        # 交叠宽 2.000001 m，按面积分配即 20.00001 人；不得吸附成 20 人。
        self.assertAlmostEqual(coverage.exact[2], 20.00001, places=9)
        self.assertNotEqual(coverage.exact[2], 20.0)
        self.assertAlmostEqual(coverage.exact[1] + coverage.exact[2], 100.0, places=9)


class InputValidationTests(unittest.TestCase):
    def test_invalid_facility_rejected_by_default(self):
        bundle = build_example_bundle()
        bundle["facilities"][0]["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [6, 10], [6, 0], [0, 10], [0, 0]]]}
        with self.assertRaises(InvalidGeometryError):
            service(bundle)

    def test_overlapping_population_cells_rejected(self):
        bundle = build_example_bundle()
        bundle["population"].append({
            "id": "other", "persons": 50,
            "geometry": {"type": "Polygon",
                         "coordinates": [[[5, 0], [15, 0], [15, 10], [5, 10], [5, 0]]]}})
        with self.assertRaises(InvalidInputError):
            service(bundle)

    def test_overlapping_multipart_population_rejected(self):
        bundle = build_example_bundle()
        bundle["population"] = [{
            "id": "mp", "persons": 30,
            "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[0, 0], [6, 0], [6, 10], [0, 10], [0, 0]]],
                [[[4, 0], [10, 0], [10, 10], [4, 10], [4, 0]]],
            ]}}]
        with self.assertRaises(InvalidInputError):
            service(bundle)

    def test_overlapping_multipart_facility_is_union(self):
        bundle = build_example_bundle()
        bundle["facilities"] = [{
            "id": "mpf", "level": 1,
            "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[0, 0], [6, 0], [6, 10], [0, 10], [0, 0]]],
                [[[4, 0], [10, 0], [10, 10], [4, 10], [4, 0]]],
            ]}}]
        coverage = service(bundle).query(["mpf"])
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.exact[1], 100)

    def test_unsupported_geometry(self):
        bundle = build_example_bundle()
        bundle["facilities"][0]["geometry"] = {"type": "Point", "coordinates": [1, 1]}
        with self.assertRaises(InvalidGeometryError):
            service(bundle)

    def test_three_dimensional_coordinates_rejected(self):
        bundle = build_example_bundle()
        bundle["facilities"][0]["geometry"]["coordinates"] = [
            [[0, 0, 1], [6, 0, 1], [6, 10, 1], [0, 10, 1], [0, 0, 1]]]
        with self.assertRaises(InvalidGeometryError):
            service(bundle)

    def test_non_equal_area_crs_rejected(self):
        bundle = build_example_bundle()
        bundle["crs"] = {"kind": "geographic", "unit": "deg"}
        with self.assertRaises(SchemaError):
            service(bundle)

    def test_missing_facility_query(self):
        svc = service(build_example_bundle())
        coverage = svc.query(["west", "ghost"])
        self.assertEqual(coverage.unknown_ids, ("ghost",))
        self.assertEqual(coverage.union, 60)

    def test_unknown_facility_only_errors(self):
        with self.assertRaises(UnknownFacilityError):
            service(build_example_bundle()).query(["ghost"])


class RepairTests(unittest.TestCase):
    def test_bowtie_repair_is_separate_and_reported(self):
        bundle = build_example_bundle()
        bundle["facilities"][0]["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [6, 10], [6, 0], [0, 10], [0, 0]]]}
        repaired, report = repair_bundle(bundle)
        self.assertEqual(report["changed_count"], 1)
        rows = [row for row in report["geometries"] if row["id"] == "west"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["valid_before"])
        self.assertTrue(row["valid_after"])
        self.assertTrue(row["topology_changed"])
        self.assertGreater(row["parts_after"], row["parts_before"])
        # 修复资料可独立查询；原始资料仍被拒绝。
        service(repaired).query(["west", "east"])
        with self.assertRaises(InvalidGeometryError):
            service(bundle)

    def test_repair_cannot_fix_overlapping_cells(self):
        bundle = build_example_bundle()
        bundle["population"].append({
            "id": "other", "persons": 50,
            "geometry": {"type": "Polygon",
                         "coordinates": [[[5, 0], [15, 0], [15, 10], [5, 10], [5, 0]]]}})
        _, report = repair_bundle(bundle)
        self.assertFalse(report["query_ready"])
        self.assertIn("InvalidInputError", report["query_error"])


class ReuseTests(unittest.TestCase):
    def test_subset_arrangement_equals_full_recompute(self):
        bundle = build_example_bundle()
        bundle["facilities"].append(
            {"id": "north", "level": 2,
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0, 6], [10, 6], [10, 10],
                                           [0, 10], [0, 6]]]}})
        svc = service(bundle)
        base = svc.arrangement(["west", "east", "north"])
        for ids in (["west"], ["west", "east"], ["north"], ["west", "east", "north"]):
            subset = base.subset(ids)
            direct = svc.arrangement(ids)
            # 子集复用的面片可能比独立重切更细，但按隶属集合聚合后必须完全一致。
            self.assertEqual(subset.membership_totals(), direct.membership_totals(), ids)
            spec = QuerySpec.from_selection(ids, None, (1, 2))
            from src.exposure_overlay.query import make_coverage

            left = make_coverage(svc.scene, subset, spec)
            right = make_coverage(svc.scene, direct, spec)
            self.assertEqual(left.union, right.union)
            self.assertEqual(left.exact, right.exact)
            self.assertEqual(left.uncovered, right.uncovered)

    def test_level_threshold_query(self):
        bundle = build_nested_bundle()
        svc = service(bundle)
        level_one = svc.query(level_min=1, thresholds=[1, 2, 3])
        level_two = svc.query(level_min=2, thresholds=[1, 2])
        self.assertEqual(level_one.facility_ids, ("a", "b", "c"))
        self.assertEqual(level_two.facility_ids, ("c",))
        self.assertEqual(level_two.union, 50)
        self.assertEqual(level_two.excluded_ids, ("a", "b"))

    def test_precompute_shares_cut(self):
        svc = service(build_nested_bundle())
        selections = [{"a"}, {"c"}, {"a", "b", "c"}]
        subset_arrangements = svc.precompute(selections)
        for selection, arrangement in zip(selections, subset_arrangements):
            direct = svc.arrangement(selection)
            self.assertEqual(arrangement.membership_totals(),
                             direct.membership_totals())

    def test_repeated_query_is_cached(self):
        svc = service(build_example_bundle())
        first = svc.arrangement(["west", "east"])
        second = svc.arrangement(["east", "west"])
        self.assertIs(first, second)


class VersionUpdateTests(unittest.TestCase):
    def test_shifted_facility_changes_expected_people(self):
        old = service(build_example_bundle())
        new = service(build_shifted_bundle(dx=1.5))
        old_coverage = old.query(["west", "east"], thresholds=[1, 2])
        new_coverage = new.query(["west", "east"], thresholds=[1, 2])
        self.assertEqual(old_coverage.exact[2], 20)
        self.assertEqual(new_coverage.exact[2], 5)
        self.assertEqual(new_coverage.union, 100)
        diff = compare_versions(old.arrangement(["west", "east"]),
                                new.arrangement(["west", "east"]),
                                old_coverage, new_coverage)
        self.assertEqual(diff["intersection_delta"], -15)
        self.assertEqual(len(diff["affected_cells"]), 1)
        self.assertEqual(diff["affected_cells"][0]["cell_id"], "cell")

    def test_old_query_still_reproducible(self):
        old = service(build_example_bundle())
        before = old.query(["west", "east"], thresholds=[1, 2])
        service(build_shifted_bundle(dx=1.5))  # 新版本查询不影响旧服务缓存
        again = old.query(["west", "east"], thresholds=[1, 2])
        self.assertEqual(before.to_dict(), again.to_dict())

    def test_incremental_matches_full_rebuild(self):
        def bundle(dx):
            result = build_two_cell_bundle("v1", dx)
            from shapely.geometry import box, mapping

            result["facilities"].append({"id": "westfac", "level": 1,
                                         "geometry": mapping(box(2, 2, 6, 8))})
            return result

        old = service(bundle(0))
        base = old.arrangement(["across", "westfac"])
        new = service(bundle(1.0))
        diff = changed_cells(old.scene, new.scene, ["across", "westfac"])
        self.assertEqual(diff["affected_cells"], ["east_cell"])
        updated, report = update_arrangement(old.scene, base, new.scene,
                                             ["across", "westfac"])
        self.assertEqual(report["reused_cells"], ["west_cell"])
        self.assertEqual(report["rebuilt_cells"], ["east_cell"])
        direct = new.arrangement(["across", "westfac"])
        self.assertEqual(updated.fingerprint(), direct.fingerprint())
        spec = QuerySpec.from_selection(["across", "westfac"], None, (1, 2))
        from src.exposure_overlay.query import make_coverage

        incremental = make_coverage(new.scene, updated, spec)
        reference = make_coverage(new.scene, direct, spec)
        self.assertEqual(incremental.union, reference.union)
        self.assertEqual(incremental.exact, reference.exact)

    def test_version_change_requires_explicit_rebuild(self):
        old = service(build_two_cell_bundle("v1", 0))
        base = old.arrangement(["across"])
        new = service(build_two_cell_bundle("v2", 1.0))
        with self.assertRaises(ValueError):
            update_arrangement(old.scene, base, new.scene, ["across"])

    def test_facility_removal_updates_slice(self):
        old = service(build_example_bundle("v1"))
        base = old.arrangement(["west", "east"])
        bundle = build_example_bundle("v1")
        bundle["facilities"] = [f for f in bundle["facilities"] if f["id"] != "east"]
        new = service(bundle)
        updated, report = update_arrangement(old.scene, base, new.scene,
                                             ["west", "east"])
        self.assertEqual(updated.facility_ids, ("west",))
        spec = QuerySpec.from_selection(["west"], None, (1, 2))
        from src.exposure_overlay.query import make_coverage

        coverage = make_coverage(new.scene, updated, spec)
        reference = make_coverage(new.scene, new.arrangement(["west"]), spec)
        self.assertEqual(coverage.union, reference.union)
        self.assertEqual(coverage.exact, reference.exact)
        self.assertEqual(report["mode"], "incremental")

    def test_facility_addition_updates_slice(self):
        old = service(build_example_bundle("v1"))
        base = old.arrangement(["west"])
        new = service(build_example_bundle("v1"))
        updated, report = update_arrangement(old.scene, base, new.scene,
                                             ["west", "east"])
        spec = QuerySpec.from_selection(["west", "east"], None, (1, 2))
        from src.exposure_overlay.query import make_coverage

        coverage = make_coverage(new.scene, updated, spec)
        reference = make_coverage(new.scene, new.arrangement(["west", "east"]), spec)
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.exact, reference.exact)
        self.assertEqual(updated.fingerprint(),
                         new.arrangement(["west", "east"]).fingerprint())


class LargeCoordinateTests(unittest.TestCase):
    """投影坐标量大时结果不应漂移。"""

    def test_canonical_example_at_projected_offsets(self):
        from shapely.affinity import translate
        from shapely.geometry import mapping, shape

        for offset in (500_000, 4_000_000, 10_000_000):
            bundle = build_example_bundle()
            for record in bundle["population"] + bundle["facilities"]:
                record["geometry"] = mapping(translate(shape(record["geometry"]),
                                                       xoff=offset, yoff=offset))
            coverage = service(bundle).query(["west", "east"], thresholds=[1, 2])
            self.assertEqual(coverage.union, 100)
            self.assertEqual(coverage.exact[1], 80)
            self.assertEqual(coverage.exact[2], 20)
            self.assertLess(abs(coverage.conservation["conservation_error"]), 1e-9)


class SampleFixtureTests(unittest.TestCase):
    def test_sample_fixture_matches_expected(self):
        coverage = service(json.loads(SAMPLE.read_text(encoding="utf-8"))).query(
            ["west", "east"], thresholds=[1, 2])
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.exact[1], 80)
        self.assertEqual(coverage.exact[2], 20)


class ConservationStressTests(unittest.TestCase):
    def test_many_cells_conservation_and_area(self):
        # 20×10 个 10 米单元，部分设施覆盖部分区域。
        from shapely.geometry import box, mapping

        cells = []
        for row in range(10):
            for column in range(20):
                cells.append({"id": f"c{row}_{column}", "persons": 7,
                              "geometry": mapping(box(column * 10, row * 10,
                                                      column * 10 + 10, row * 10 + 10))})
        bundle = {
            "schema_version": 1,
            "population": cells,
            "facilities": [
                {"id": "big", "level": 1,
                 "geometry": mapping(box(5, 5, 155, 65))},
                {"id": "band", "level": 2,
                 "geometry": mapping(box(0, 30, 200, 50))},
            ],
            "crs": {"kind": "equal_area_planar", "unit": "m"},
            "version": "stress",
        }
        svc = service(bundle)
        coverage = svc.query(["big", "band"], thresholds=[1, 2])
        total = 200 * 7
        self.assertEqual(coverage.population_total, total)
        conservation = coverage.conservation
        self.assertTrue(conservation["conservation_within_tolerance"])
        self.assertLess(abs(conservation["conservation_error"]), 1e-9)
        self.assertLess(abs(conservation["area_conservation_error"]), 1e-6)
        rows = svc.ledger(coverage)
        self.assertEqual(ledger_totals(rows)["persons"], total)
        # 各重数之和 + 未覆盖 == 总人口。
        self.assertAlmostEqual(
            sum(coverage.exact.values()) + coverage.uncovered,
            total, places=9)


class RandomisedEquivalenceTests(unittest.TestCase):
    """随机场景下，面片人数必须与独立计算的容斥结果一致。"""

    @staticmethod
    def _exactly_k_area(clips, k):
        import itertools
        import math as _math

        import shapely

        total = 0.0
        for r in range(k, len(clips) + 1):
            coefficient = ((-1) ** (r - k)) * _math.comb(r, k)
            subtotal = 0.0
            for subset in itertools.combinations(range(len(clips)), r):
                intersection = clips[subset[0]]
                for index in subset[1:]:
                    intersection = shapely.intersection(intersection, clips[index],
                                                        grid_size=1e-6)
                subtotal += shapely.area(intersection)
            total += coefficient * subtotal
        return total

    def test_random_rectangles_match_inclusion_exclusion(self):
        import random

        import shapely
        from shapely.geometry import box, mapping

        random.seed(20240921)
        for _ in range(25):
            cells = []
            for row in range(2):
                for column in range(2):
                    cells.append({
                        "id": f"c{row}{column}", "persons": random.randint(1, 100),
                        "geometry": mapping(box(column * 10, row * 10,
                                                column * 10 + 10, row * 10 + 10))})
            facilities = []
            for index in range(4):
                x = random.uniform(0, 15)
                y = random.uniform(0, 15)
                facilities.append({
                    "id": f"f{index}", "level": random.randint(1, 2),
                    "geometry": mapping(box(x, y, x + random.uniform(1, 8),
                                            y + random.uniform(1, 8)))})
            bundle = {"schema_version": 1, "population": cells,
                      "facilities": facilities,
                      "crs": {"kind": "equal_area_planar", "unit": "m"},
                      "version": "random"}
            svc = service(bundle)
            ids = [facility["id"] for facility in facilities]
            arrangement = svc.arrangement(ids)
            grid = svc.tolerance.grid_size_m
            for cell in svc.scene.cells:
                pieces = arrangement.cells.get(cell.id)
                clips = [shapely.set_precision(
                    shapely.intersection(facility.geom.geometry, cell.geom.geometry,
                                         grid_size=grid), grid)
                    for facility in svc.scene.facilities]
                for k in range(1, len(ids) + 1):
                    reference = cell.persons * self._exactly_k_area(clips, k) / cell.geom.area
                    got = math.fsum(piece.persons for piece in (pieces.pieces if pieces else ())
                                    if len(piece.facility_ids) == k)
                    self.assertAlmostEqual(got, reference, places=9)


class StaticFixtureTests(unittest.TestCase):
    """静态样例与生成器保持一致，CLI 可直接使用。"""

    FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

    def test_example_fixture(self):
        svc = ExposureService.load(self.FIXTURES / "example.json")
        coverage = svc.query(["west", "east"], thresholds=[1, 2])
        self.assertEqual(coverage.union, 100)
        self.assertEqual(coverage.exact[1], 80)
        self.assertEqual(coverage.exact[2], 20)

    def test_shifted_fixture_matches_generator(self):
        loaded = json.loads((self.FIXTURES / "shifted.json").read_text(encoding="utf-8"))
        generated = json.loads(json.dumps(build_shifted_bundle("example-v2", 1.5)))
        self.assertEqual(loaded, generated)

    def test_invalid_fixture_rejected_and_repairable(self):
        with self.assertRaises(InvalidGeometryError):
            ExposureService.load(self.FIXTURES / "invalid_bowtie.json")
        loaded = json.loads((self.FIXTURES / "invalid_bowtie.json").read_text(encoding="utf-8"))
        _, report = repair_bundle(loaded)
        self.assertTrue(report["query_ready"])
        self.assertEqual(report["changed_count"], 1)

    def test_overlap_fixture_rejected(self):
        with self.assertRaises(InvalidInputError):
            ExposureService.load(self.FIXTURES / "invalid_overlap.json")


if __name__ == "__main__":
    unittest.main()
