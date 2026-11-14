# 重叠环境影响区的人口暴露联合统计

面向矢量资料的人口暴露统计组件。人口为互不重叠的面单元（每单元一个人数），
设施影响区为带孔洞的 `Polygon` / `MultiPolygon`，坐标统一在米制等面积平面。
统计按**实际交叠面积**分配人口，不按质心落点决定整单元归属。

基准结果（`fixtures/example.json`）：面积 100 的正方形、人口 100，两个影响矩形
各覆盖 60、交叠 20 → **并集 100，恰好一处 80，恰好两处 20**。该结果由三块面片
（40 / 20 / 40）直接相加得到，可在面片台账中逐片复核。

## 运行

依赖 Shapely 2.x（GEOS 3.x）；无其它第三方依赖，Python 3.11+。

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q src tests

命令行：

    python3 -m src.exposure_overlay.cli example
    python3 -m src.exposure_overlay.cli query fixtures/example.json \
        --facilities west,east --thresholds 1,2 --ledger
    python3 -m src.exposure_overlay.cli query fixtures/nested.json --level-min 2
    python3 -m src.exposure_overlay.cli ledger fixtures/hole.json --facilities ringed
    python3 -m src.exposure_overlay.cli repair fixtures/invalid_bowtie.json
    python3 -m src.exposure_overlay.cli compare fixtures/example.json fixtures/shifted.json \
        --facilities west,east
    python3 -m src.exposure_overlay.cli render fixtures/nested.json /tmp/nested.svg \
        --facilities a,b,c --thresholds 1,2,3

## 空间模型与取舍

### 几何库
选择 **Shapely 2 / GEOS**：它提供可靠的平面布尔运算、`polygonize` 平面排列、
`STRtree` 候选索引，以及显式的 `set_precision` / `make_valid` 语义。没有在
Python 里自行实现求交，因为浮点鲁棒性是主要风险点，交给成熟的 GEOS 更可控。

### 精度模型
- 所有几何吸附到统一的 `Tolerance.grid_size_m`（默认 **1e-6 m**，即微米）网格，
  使用 `shapely.set_precision(..., mode="valid_output")`。吸附后坐标可精确表示，
  布尔运算结果可重复。
- 几何判定容差为 `max(grid², 0.5·grid·周长)`（平方米）；只有交叠面积大于该值
  才算“实质交叠”。**容差只用于判定，不用于人数取整**。
- 人口分配：`面片人数 = 单元人数 × 面片精确面积 / 单元面积`，不做逐片取整。
  单元内部均匀分布是明确的估算假设，报告里保留 `persons_per_m2` 以便复核。
- 总量用 `math.fsum` 在面片层面一次性求和，避免逐片舍入后累加。
- 在 1e7 m 量级的投影坐标下实测面积偏差约 1e-8 m²（吸附网格决定）；1e-6 m² 的
  面积账目容差覆盖该量级。

### 分片表示
对每个单元，把所有选中设施的影响面与单元求交得到片段（clip），再把“单元边界 +
片段边界”做 `polygonize`，得到不可再分的**面片（piece）**。面片对整个选中集合只有
“整片在内 / 整片在外”两种情况，隶属集合唯一。面片人口由精确面积按单元密度分配。

`Arrangement` 是这套切片的冻结结果：`cells → pieces`，每个面片带
`facility_ids / area / persons / geometry` 与稳定编号。`Coverage` 只做隶属集合
的位运算，不重算几何。

### 覆盖语义
- **并集**：被至少一个选中设施覆盖的人口（面片 `k ≥ 1`）。
- **交集**：被全部选中设施覆盖的人口（面片 `k = |选中|`）。
- **恰好 k 处**：`k` 个选中设施覆盖、且不被其余选中设施覆盖的人口。
- **至少 k 处**：`k ≥ 阈值` 的人口（`thresholds` 可给多个）。
- **未覆盖**：`k = 0` 的面片人口 + 完全不受影响单元的全部人口。
- 孔洞、单元孔洞与单元内部不计覆盖；边界接触面积为零。
- 多部件影响面自身重叠先取并；人口单元间实质重叠属非法输入，直接拒绝。

### 空间候选与精确相交
`STRtree` 的命中只作为候选对；每一对候选都做精确求交并比较面积。
报告分别给出 `candidate_pairs`、`exact_pairs`、`degenerate_pairs`，以及每条的
`near_misses`（候选命中但仅共边/近邻、面积不超容差）。近邻命中**不是**相交证据。

### 复用
- `ExposureService.arrangement(ids)` 缓存整次切片。
- `arrangement.subset(ids)` 在更大切片上做隶属集合裁剪，不重算几何。
- `ExposureService.precompute([...])` 对设施并集切一次，再给出多个子集切片。
- `subset` 的面片可能比独立重切更细，但 `Arrangement.membership_totals()`（按
  “单元 + 隶属集合”聚合）与独立切片完全一致；测试在多个子集上核对这一点。

### 增量与版本
- `changed_cells(old_scene, new_scene, ids)` 用新旧几何的对称差定位受影响人口单元。
- `update_arrangement(...)` 只重切受影响单元，其余复用旧切片；结果与整体重切
  的 `fingerprint()` 一致。人口几何/人数/总量或资料版本变化时要求
  `full_rebuild=True`，否则拒绝。
- 旧 `Arrangement` / `Coverage` 保持不可变，旧查询可按旧版本复现；`compare_versions`
  给出按单元的并集/交集/重数差异。

### 无效几何与修复
- 默认**拒绝**原始无效几何（自交、孔洞穿壳、空/退化、第三维、非面几何），错误
  指出 `role:id` 与 GEOS 的原因。
- `repair_bundle(bundle)` 是独立方案：用 `make_valid` 修复后吸附，报告每个几何的
  `area_before/after`、部件数、孔洞数、顶点数与 `topology_changed`，并给出修复
  资料是否 `query_ready`。正式查询不会静默使用修复结果。

## 模块

    src/exposure_overlay/core.py        资料校验、几何归一化、修复方案
    src/exposure_overlay/arrangement.py 平面切片、面片与复用
    src/exposure_overlay/query.py       覆盖语义、守恒账目、版本对比
    src/exposure_overlay/update.py      设施换版增量
    src/exposure_overlay/service.py     面向调用方的服务 API
    src/exposure_overlay/cli.py         命令行
    src/exposure_overlay/examples.py    内置场景
    src/exposure_overlay/render.py      SVG 复核视图（非统计路径）
    src/exposure_overlay_input.py       原始资料读取（沿用）

## 内置场景

| 场景 | 期望 |
| --- | --- |
| `example` | 并集 100，恰好 1 处 80，恰好 2 处 20 |
| `hole` | 孔洞 16 人未覆盖，覆盖 84 |
| `multipart` | 多部件设施/人口，覆盖 64、未覆盖 16 |
| `containment` | 完全包含：并集 100，恰好 1 处 60，恰好 2 处 40 |
| `shared_edge` | 仅共边：交集 0，恰好 1 处 100 |
| `nested` | 三设施：恰好 1/2/3 处 40/50/10 |
| `shifted` | east 右移 1.5 m：交集 20 → 5 |

每个场景都可导出面片台账（`--ledger`）与 SVG（`render_svg`）。
