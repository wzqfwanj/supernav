# SuperNav「5 种交通方式对比」— 独立验证报告（task-6）

- 验证角色：verifier（独立于 backend / frontend 实现，只验证、不改业务代码）
- 说明：本报告主体为 **v0.2.0 5 方式对比** 阶段验证；最新发布前复验见 **[`verify/v0.4.0-report.md`](v0.4.0-report.md)**（v0.4.0 最终版：smoke 4187 PASS / 0 WARN、DOM 115 PASS、pytest 80 passed；v0.3.0 起点定位报告见 [`verify/v0.3.0-report.md`](v0.3.0-report.md)）。
- 首次验证：2026-11-18 13:04–13:07（Asia/Shanghai）
- **v0.2.0 发布前复验：2026-11-18 13:09（L-compare-1 修复 + 版本升 0.2.0 后重跑，结论 PASS）**
- 验证对象：`POST /api/compare`（契约 3.4）、`/api/plan` 回归、前端方式对比/组合行程 UI
- 服务：`http://127.0.0.1:8014`（脚本自行起停；8000 由 Lead 占用，全程未使用）
- 运行模式：**演示模式**（无 `AMAP_KEY`、无 `DEMO_MODE`、无 `config.local.json`；全程不访问真实高德接口）
- 交付物：
  - `verify/smoke.py`（已扩展 `/api/compare` 契约/费用/失败隔离/前端对比逻辑检查）
  - `verify/dom_check.py`（新增：Playwright + 本机 Edge 无头浏览器真实点击 + DOM 断言）
  - `verify/compare-report.md`（本报告）、`verify/artifacts/`（证据 JSON/截图/日志，已被 `.gitignore` 忽略）

## 0. 结论

| 项目 | 结果 |
| --- | --- |
| 总体结论 | **PASS**（无严重/中等缺陷；L-compare-1、L-compare-3 已修复并复验通过） |
| `python verify/smoke.py --auto-server` | **PASS=4099 / FAIL=0 / WARN=0，退出码 0**（首次 + 最终复验均一致；含 `/api/plan` 全量回归 + `/api/compare`） |
| `python verify/dom_check.py --auto-server` | **PASS=90 / FAIL=0，退出码 0**（首次 + 最终复验；无头 Edge 真实打开页面、模拟点击、读取 DOM） |
| 后端 pytest（离线 mock） | **75 passed**（compare 相关：`TestCompareLive` 7 passed；失败隔离 `-k compare_isolates` 3 passed） |
| node --check | `web/app.js` rc=0；index.html 内联脚本 rc=0 |
| 严重 / 中等缺陷 | **0 / 0**；仅剩 1 条信息级观察项（L-compare-2，见 §10） |
| 安全 | `/api/compare` 响应与前端源码未发现 Key 泄漏；`.gitignore` 覆盖 `config.local.json` 等既有检查保持 PASS |
| 前端实测 | 5 张方式卡顺序、逐卡切换详情、available=false 错误态（普通点击可达，无 aria-disabled 矛盾）、组合行程、演示横幅、三主题 localStorage 均通过 DOM 实测 |

## 1. 环境与命令

| 组件 | 版本 |
| --- | --- |
| Python / Node.js | 3.12.10 / v24.21.0 |
| FastAPI / uvicorn / requests / pytest | 0.162.0 / 0.54.0 / 2.34.2 / 9.2.0 |
| 服务版本（`/api/status`） | **0.2.0**（发布前最终版本） |
| 无头浏览器 | Playwright 1.64.1 + 本机 Microsoft Edge（`msedge.exe`，`executable_path` 方式，无需下载浏览器） |
| 高德 Key | 无（演示模式） |

> 首次验证：2026-11-18 13:04–13:07（版本 0.1.1）；发布前最终复验：13:09（版本 0.2.0，L-compare-1 修复后）。下方命令与结果均为最终复验值。

实际执行命令（PowerShell，项目根目录）：

```powershell
# 1) 后端 + 前端静态 + 回归 + compare 契约/费用（脚本自行拉起并关闭 8014）
python verify/smoke.py --auto-server
# 统计：PASS=4099  FAIL=0  WARN=0；退出码 0

# 2) 无头浏览器 DOM 交互（同样自行起停 8014，Playwright 驱动本机 Edge）
python verify/dom_check.py --auto-server
# 统计：PASS=90  FAIL=0；退出码 0

# 3) 后端单测（离线 mock，不联网）
python -m pytest -q                                     # 75 passed
python -m pytest -q -k "compare_isolates"               # 3 passed（单方式失败隔离）
python -m pytest -q -k "TestCompareLive"                # 7 passed（真实模式 compare mock）
node --check web/app.js                                 # rc=0
```

> 8000 端口始终由 Lead 的进程占用，本验证只用 8014；验证结束后 8014 无监听（仅 TIME_WAIT 残留），8000 未被触碰。

## 2. 结果总览（smoke 4099 项分布）

| 分组 | PASS | FAIL |
| --- | ---: | ---: |
| 1. 健康 / 状态（`/api/healthz`、`/api/status`） | 17 | 0 |
| 2. `/api/plan` 回归：2–5 段混合 + location + 10 点 + options | 804 | 0 |
| 2b. `/api/plan` 参数校验（4xx） | 127 | 0 |
| 3. `/api/compare` 功能与费用（2/3/4/5/10 点、options、location、忽略 modes） | 2968 | 0 |
| 4. `/api/compare` 参数校验（4xx、错误格式） | 52 | 0 |
| 5. compare 单方式失败隔离（代码审查 + 定向 mock 单测） | 18 | 0 |
| 6. 安全（含 `/api/compare` 响应扫描） | 28 | 0 |
| 7. 前端静态资源 | 19 | 0 |
| 8. 前端关键逻辑代码审查（含对比/组合模式） | 60 | 0 |
| 9. `node --check` | 3 | 0 |
| 10. 全量 pytest | 2 | 0 |
| 其他（服务可达） | 1 | 0 |
| **合计** | **4099** | **0** |

原始证据：`verify/artifacts/smoke-run.json`、`verify/artifacts/dom-check.json`、`verify/artifacts/final-smoke.txt`、`verify/artifacts/final-dom.txt`、`verify/artifacts/dom-compare.png`（对比页全页截图，仅作证据）。
**最终复验快照** SHA256（16 位前缀，完整表见 `verify/artifacts/source-sha256-final.json`）：`backend/app.py 75e744dd3844b651`、`backend/planner.py 6c3796581c42bb6f`、`backend/amap.py 1d7c4e6200372d4c`、`backend/__init__.py 47a8e0a3a9b8664d`（0.2.0）、`tests/test_backend.py 02f8ba33967c1176`、`web/app.js 72c9ae77aba84536`（aria-disabled 已移除）、`web/index.html d3aea7405de52deb`、`web/styles.css 8c994519d0b47f2b`、`docs/API-CONTRACT.md 3218b3613186d33b`（3.1 示例 0.2.0）、`README.md 852cae204386dccf`（已同步 v0.2/计数）。与首次验证快照相比仅 4 个文件变化：`backend/__init__.py`（版本）、`web/app.js`（aria-disabled 移除）、`docs/API-CONTRACT.md`（版本示例）、`README.md`（文档同步）；后端功能代码与测试未变。

## 3. `/api/compare` 契约与费用（PASS）

### 3.1 结构与顺序

- `POST /api/compare`（2–10 个点，分别实测 2/3/4/5/10 点）均 `200`；顶层键恰为 `{"mode","generated_at","points","modes"}`；`mode="demo"`、`generated_at` 为带时区 ISO 8601。
- `modes` 固定 5 项，顺序为 **walking、bicycling、transit、taxi、driving**（与契约 3.4 一致）；每项键恰为 `{"mode","mode_label","available","error","legs","totals","summary"}`；演示模式 5 项 `available=true`、`error=null`。
- 每项 `legs` 长度 = 点数−1（2/3/4/5/10 点分别 1/2/3/4/9 段），且每段 `legs[].mode` 均为该方式；`points` 结构与 `/api/plan` 一致。
- `points` 支持 `address` 与直接 `location` 经纬度（实测 `116.378,39.865` 原样回显、名称回显）；请求无需 `modes`，传入非法 `modes:["flying"]` 与未知字段会被忽略且仍返回 5 项。

3 点实测（北京南站→故宫→北京西站）：

| 方式 | 段数 | distance_m | duration_s | cost.total | 费用明细 / summary |
| --- | ---: | ---: | ---: | ---: | --- |
| walking | 2 | 14952 | 11961 | 0.0 | 空明细；summary 空 |
| bicycling | 2 | 15602 | 4680 | 0.0 | 空明细；summary 空 |
| transit | 2 | 18252 | 4540 | 8.0 | transit_fare 8.0（两段 4+4）；summary 含步行/地铁/公交换乘 |
| taxi | 2 | 16902 | 1521 | 51.08 | taxi_fare 51.08 |
| driving | 2 | 16902 | 1521 | 10.55 | fuel 10.55（两段里程均 <20km，无过路费） |

### 3.2 合计与费用不变式（逐段独立复算）

- 每项 `totals.distance_m/duration_s` = 该项逐段之和；`totals.cost` 按 key 合并逐段明细；`cost.total == sum(breakdown.amount)`（按分比较，误差 0）；金额均 2 位小数。
- 逐段费用用响应 `distance_m` 独立复算（driving 油费/过路费、taxi 估算、transit 票价阶梯、walking/bicycling=0），覆盖 2/3/4/5/10 点全部 95 段（5 方式 × 19 段）**0 处不匹配**。
- 语义交叉检查：walking/bicycling 每个 entry `total=0`、`breakdown=[]`；transit/taxi/driving 在距离 >0 时 `total>0`。
- `options` 生效：北京南站→首都机场 driving，`fuel_price=8.5、fuel_consumption=6.7` → 距离 26373m、油费复算 `26.373×6.7/100×8.5=15.0194→15.02`、过路费 `(26.373−20)×0.6=3.8238→3.82`、total 18.84，与接口一致。
- 费用公式独立复算示例（默认配置，3 点第 1 段，即北京南站→故宫）：transit 段 8640m → ≤12km 档 4 元（第 2 段 9612m 同为 4 元，合计 8.0）；taxi 段 7946m → `13+max(0,7.946−3)×2.3=24.3758→24.38`（第 2 段 26.70，合计 51.08）；driving 段 7946m → 油费 `7.946×8/100×7.8=4.9583→4.96`、无过路费（第 2 段 5.59，合计 10.55）；均与接口逐项一致（完整值见 `smoke-run.json` 的 `samples.compare_3points/compare_options_driving`）。

### 3.3 参数校验

| 用例 | HTTP | error.message |
| --- | ---: | --- |
| 仅 1 个点 | 400 | 至少需要起点和终点 |
| 11 个点 | 400 | 最多支持 10 个地点 |
| points 非数组 | 422 | 请求参数格式不正确（已包装统一 error） |
| 空地址 / 缺点 | 400 | 第 N 个地点缺少地址 |
| location 非法 | 400 | location 格式/经纬度错误（与 /api/plan 同规则） |
| `transit_strategy=9` / `fuel_price=-1` | 400 | options 越界提示（可读） |
| 畸形 JSON | 422 | 请求参数格式不正确（统一 `error` 结构，无裸 detail 漏出） |

## 4. 单方式失败隔离（PASS）

演示模式下 5 种方式全部可用，无法从 HTTP 自然触发 `available=false`，因此按任务要求采用**代码审查 + 既有 mock 单测 + 前端响应拦截**三重证据：

1. **代码审查**（`backend/planner.py` `compare()` / `_unavailable_mode()` / `_empty_totals()`）：
   - 对 5 种方式逐个 `try`；`except ApiError` 与 `except Exception` 均收敛为单项 `available=false`，整体仍 200；
   - 不可用项结构为 `error:{code,message,hint}`、`legs=[]`、`totals={distance_m:0,duration_s:0,cost:{total:0.0,breakdown:[]}}`、`summary=""`；可用项 `available=true,error=null`；
   - 地点解析在 5 方式循环之前完成（地理编码只一次、5 方式共享）。
2. **定向 mock 单测**：`pytest -q -k "compare_isolates"` → **3 passed**（transit/bicycling 两种 AMAP_ERROR 隔离参数化 + 意外异常隔离）；`pytest -q -k "TestCompareLive"` → **7 passed**（含 geocode 去重、steps 解析、错误隔离）。
3. **前端拦截响应实测**（DOM）：拦截 `/api/compare` 令 transit `available=false` → 卡片出现「无可用路线」徽标；默认自动选中第一个可用方式（driving）并展示详情；点 transit 卡 → `#mode-error` 显示「「公交/地铁」高德未返回可用路线…；错误码：AMAP_ERROR」且详情隐藏；再点 driving → 详情恢复。控制台/页面 JS 错误 0。

## 5. 真实模式代码路径与单测覆盖（PASS，按任务不联网）

- 代码审查：`planner.compare()` 真实分支先 `resolve_points_live(points_spec, client)` 解析一次（内部按地址去重），再为 5 种方式逐段复用 `AmapClient`；步行/骑行/驾车/打车使用 v5 direction 接口，公交使用 transit/integrated。
- 单测证据（全部离线 mock，不联网）：
  - `test_compare_geocodes_once_shared_by_all_modes`：3 点（2 个不同地址）→ geocode 仅 2 次，5 方式共享；taxi+driving 共 4 次 driving 调用，全部 `available=true`；**通过**。
  - `test_compare_live_steps_and_summary`：walking/bicycling/taxi/driving 的 `steps[].type` 分别为对应方式、最多 10 条，`instruction/road_name/step_distance/cost.duration/嵌套 polyline` 容错解析；transit 仍为乘车步骤；**通过**。
  - 失败隔离（见 §4）、geocode 失败整体 4xx（`GEOCODE_FAILED`）均有单测覆盖。
- 结论：真实分支的关键行为（地理编码一次、steps 解析、错误隔离）有代码审查 + mock 单测双重证据；未做真实高德联网联调（按任务要求）。

## 6. `/api/plan` 回归（PASS）

- `smoke.py` 原有 plan 用例全部保留并通过：2–5 段混合方式（walking/bicycling/transit/taxi/driving 组合）、逐段字段/合计/费用不变式、10 点上限、location 直接坐标、options 生效、25 组非法输入 4xx、统一错误结构。
- 931 项 plan 相关检查（804 + 127）全部 PASS，说明 `/api/compare` 依赖的 `planner` 重构没有破坏原规划接口。
- 前端组合行程路径也在 DOM 中实测（见 §8.3）。

## 7. 前端静态与代码审查（PASS）

- 静态资源：`GET /` 200 html、`styles.css` 200 css、`app.js` 200 JS、favicon 200、`/web/app.js` 兼容路径 200、未知路径 404 统一错误；`node --check` app.js 与 index 内联脚本 rc=0。
- 默认「方式对比」：`state.planMode=PLAN_MODE_COMPARE`、`#plan-mode-switch` 中「方式对比」`aria-pressed=true`、「组合行程」false；对比模式下 `renderForm()` 不渲染每段方式下拉（DOM 实测 select 数 0），首页提示文案正确。
- 5 张卡：`COMPARE_ORDER = ['transit','driving','taxi','bicycling','walking']`（公交→驾车→打车→骑行→步行）；卡片为 `<button>`，含 `data-compare-mode`、方式名、`耗时/里程/费用` 三项指标、可选 summary、选中态 `is-selected`；点击调用 `selectCompareMode()`。
- 详情复用：`renderCompareDetail()` 组装出与 `/api/plan` 相同的 `{mode,generated_at,points,legs,totals}` 后复用 `renderTotals()` / `renderLegs()` / `renderRoute()`；顶部显示 `#detail-mode` 方式徽标与 summary。
- 不可用态：卡片 `is-unavailable` + 「无可用路线」徽标，点击后 `#detail` 隐藏、`#mode-error` 展示 `error.message/hint/code`；再点可用方式恢复详情。L-compare-1 修复后该卡不再设置 `aria-disabled`，普通点击/读屏/自动化均可正常操作（DOM 实测）。
- 组合行程仍可用：`setPlanMode('combine')` 后隐藏结果、恢复每段方式下拉、`buildPlanPayload()` 携带 modes、提交 `/api/plan` 正常渲染详情；切回对比模式同样可用。
- 演示横幅/来源：`updateSourceBadge(data.mode)` 在对比/组合结果中均生效，`mode="demo"` 时显示「演示数据」并保证演示横幅可见。
- 主题/localStorage：`THEME_KEY='supernav-theme'`、三值 light/gray/dark、`data-theme`、首屏内联脚本读取、点击持久化；DOM 实测暗色切换 + reload 持久化 + 切回明色成功。
- 安全写法：前端无 `innerHTML/outerHTML/insertAdjacentHTML/document.write/eval`；无外部 CDN 外链（唯一 `http://` 为 SVG 命名空间）；无 Key 字符串。

## 8. 无头浏览器 DOM 交互实测（PASS=90，无头 Edge）

环境：Playwright 1.64.1 驱动本机 `msedge.exe`（headless，1440×1100），真实加载 8014 页面并模拟填写/点击；`console errors=[]`、`pageerror=[]`。

### 8.1 对比页与 5 张卡

| 检查 | 实测结果 |
| --- | --- |
| 演示横幅 | 可见 |
| 默认模式 | 方式对比（aria-pressed=true），每段下拉 0 个 |
| 卡片顺序 | `["transit","driving","taxi","bicycling","walking"]` |
| 卡片名称 | 公交/地铁、驾车、打车、骑行、步行 |
| 卡片指标 | 每卡「耗时/里程/费用」3 项且均有数值 |
| 来源徽标 | 演示数据 |

### 8.2 点击切换详情（数值与 `/api/compare` 响应逐项一致）

2 点（北京南站→故宫博物院）逐卡点击实测：

| 方式 | 详情标签 | 总里程 | 总耗时 | 总路费 | 段数 |
| --- | --- | --- | --- | --- | ---: |
| 公交/地铁 | 公交/地铁 | 8.64 公里 | 37 分钟 | ¥4.00 | 1 |
| 驾车 | 驾车 | 7.95 公里 | 12 分钟 | ¥4.96 | 1 |
| 打车 | 打车 | 7.95 公里 | 12 分钟 | ¥24.38 | 1 |
| 骑行 | 骑行 | 7.33 公里 | 37 分钟 | ¥0.00 | 1 |
| 步行 | 步行 | 7.03 公里 | 1 小时 34 分钟 | ¥0.00 | 1 |

- 每次点击后仅 1 张卡 `is-selected`；详情合计/费用明细/逐段列表/路线示意（legend 段数）同步切换；transit 详情显示换乘 summary。
- 路线示意：5 种方式详情均显示 `#route-section`，legend 段数与响应一致。

### 8.3 available=false / 组合行程 / 错误与主题

- **不可用态**：拦截响应使 transit `available=false` → 卡片「无可用路线」徽标；自动选中 driving 展示详情；点击 transit（DOM 实测该卡 **`aria-disabled` 已移除**、原生 `disabled=false`，Playwright 普通 click 通过，不再需要 force click）显示 `#mode-error`（含方式名、错误 message、hint、错误码 AMAP_ERROR），详情隐藏；切回 driving 详情恢复。
- **组合行程**：切换到「组合行程」后结果隐藏、方式下拉恢复（1 个）；选「打车」提交 `/api/plan` → 200，1 段详情，合计 `7.95 公里 / 12 分钟 / ¥24.38`，组合 chips「组合：打车」、路线 legend 1 段；对比区隐藏、`#detail-mode` 隐藏。
- **表单错误**：清空起点提交 → `#alert` 展示「请填写起点的地址。」，关闭后隐藏。
- **主题持久化**：点击「暗」→ `html[data-theme=dark]` 且 `localStorage.supernav-theme=dark`；reload 后仍为 dark；切回「明」→ light。

## 9. 安全（PASS）

- `/api/compare` 响应（2 点、10 点与 options/location 用例）纳入 Key 泄漏扫描：无 32 位十六进制 Key 值、无 `restapi.amap.com`、无 `"amap_key":`、无 query `key=`；`demo_reason` 中的环境变量名 `AMAP_KEY` 属说明文案（既有误报已排除）。
- 前端 `index.html/app.js/styles.css` 无 `AMAP_KEY/amap_key/restapi.amap.com`、无硬编码 32 位 Key；后端 `*.py` 无硬编码 Key。
- `.gitignore` 含 `config.local.json` 与 `.env`；工作区无 `config.local.json`；`config.local.example.json` 为占位符；`/api/status` 响应键仍为契约 6 字段。

## 10. 发现的问题（按严重度分级）

**严重（阻塞验收）：无。**
**中等：无。**

低风险 / 信息级观察项：

| 编号 | 级别 | 描述 | 位置 / 建议 |
| --- | --- | --- | --- |
| L-compare-1 | **已修复（复验 PASS）** | 原问题：不可用方式卡同时 `aria-disabled="true"`、绑定 click 看原因、title 提示点击，ARIA 语义与交互矛盾（读屏/Playwright 视为 disabled）。Lead 已移除 `aria-disabled`（保留 `is-unavailable` 样式 + click + title 提示）。最终复验：DOM 实测卡片 `aria_disabled=false, native_disabled=false`，Playwright **普通 click 通过**（无需 force），错误态（#mode-error 显示原因、详情隐藏、切回恢复）正常 | `web/app.js` `renderCompareCards`；证据 `verify/artifacts/dom-check.json` |
| L-compare-2 | 信息（契约「建议」项） | 演示模式下 walking/bicycling/taxi/driving 的 `legs[].steps` 为空、entry `summary` 为空（契约 3.4 对非公交 steps 是「建议」；真实模式已实现最多 10 条指令）。前端对空 steps/summary 有兜底，详情仍展示距离/耗时/费用/polyline，不影响验收 | `backend/demo.py` `route()` 与 `planner._mode_summary`；可后续给演示数据补道路摘要 |
| L-compare-3 | **已修复（复验 PASS）** | 版本已升 `0.2.0`（`/api/status` 实测 0.2.0）；`docs/API-CONTRACT.md` 3.1 示例已同步 `"version": "0.2.0"`；`README.md` 已同步 v0.2（5 方式对比 + 组合行程）与当前计数（75 passed / 4099 PASS / DOM 90 PASS / v0.2.0 Release）。README 在验证期间由 Lead 并发更新（最终 hash `852cae20…`），属文档变更，不影响功能快照 | `docs/API-CONTRACT.md:36`、`README.md:8/112/114` |

补充（验证工具维护，非产品缺陷）：`verify/smoke.py` 因版本号从 0.1.1 → 0.2.0、以及前端请求封装重构（`fetch('api/status')`、`requestEndpoint('api/compare')`）需同步更新旧断言；`verify/dom_check.py` 原先把 `aria-disabled="true"` 当作预期（L-compare-1），本次改为断言其不存在 + 普通 click。更新后最终复验 smoke 4099/0、DOM 90/0。

## 11. 复现 / 复验方式

```powershell
# A. 后端接口 + 前端静态/源码 + /api/plan 回归 + /api/compare（自动起停 8014）
python verify/smoke.py --auto-server
# 期望：统计：PASS=4099  FAIL=0  WARN=0；退出码 0

# B. 无头浏览器 DOM 交互（Playwright + 本机 Edge，自动起停 8014）
python verify/dom_check.py --auto-server
# 期望：统计：PASS=90  FAIL=0；退出码 0

# C. 离线单测
python -m pytest -q                        # 75 passed
python -m pytest -q -k "compare_isolates or TestCompareLive"

# D. 前端语法
node --check web/app.js                    # rc=0
```

L-compare-1 已修复并完成发布前最终复验（见 §0/§10）；若后续再改动业务代码，重跑 A、B（及 C）并更新本报告与 `verify/artifacts/` 即可。

## 12. 工作区完整性与验证局限

**完整性**：verifier 只写 `verify/**`（修改 `verify/smoke.py`、`verify/dom_check.py`，新增/更新 `verify/compare-report.md`、`verify/artifacts/*`），未修改 `backend/`、`web/`、`tests/`、`docs/`、`README.md`、`deploy/` 等业务/文档文件；未执行 git 写操作。验证期间业务文件由 backend/frontend/Lead 并行修改；首次验证快照见 `verify/artifacts/source-sha256-compare.json`，**发布前最终快照见 `verify/artifacts/source-sha256-final.json`**（相对首次仅 4 个文件变化：`backend/__init__.py` 版本、`web/app.js` 移除 aria-disabled、`docs/API-CONTRACT.md` 版本示例、`README.md` 文档同步；后端功能代码与 tests 未变）。本次结论对应该最终快照。

**局限**：
- 演示模式离线验证（无真实 Key、不联网）；真实模式 compare 仅代码审查 + mock 单测，无真实高德联调。
- 无头验证使用本机 Edge（非 Playwright 下载的 chromium 版本）；available=false 场景通过响应拦截模拟（非真实高德故障），后端失败隔离由 mock 单测覆盖。
- 未做移动端/多浏览器视觉与像素级对比、性能/并发/长跑测试；截图仅作证据，不作为视觉断言。
