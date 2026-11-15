# SuperNav 交通规划 MVP — 独立验证报告（task-3）

- 验证角色：verifier（独立于 backend / frontend 实现，只验证、不改业务代码）
- 首次验证：2026-11-15 13:08–13:10（Asia/Shanghai）
- L1 修复后复验：2026-11-15 13:12（Asia/Shanghai；Lead 修复真实模式地铁 `steps.type` 后重跑）
- 验证对象：`backend/` + `web/` + 契约 `docs/API-CONTRACT.md`，服务实际运行在 `http://127.0.0.1:8014`
- 运行模式：**演示模式**（环境未配置 `AMAP_KEY`、未设置 `DEMO_MODE`、无 `config.local.json`；全程不访问真实高德接口）
- 交付物：`verify/smoke.py`（可重复运行、自带断言、退出码 0/1）、`verify/report.md`、`verify/artifacts/`（证据 JSON/日志，已被 `.gitignore` 忽略）

## 0. 结论

| 项目 | 结果 |
| --- | --- |
| 总体结论 | **PASS**（无阻塞/中等缺陷；L1 已由 Lead 修复并复验通过） |
| `verify/smoke.py` 检查 | **PASS=1040，FAIL=0，WARN=0，退出码 0**（首次 + 复验均一致） |
| 后端 pytest（离线 mock） | **57 passed**（0 failed；复验后仍为 57 passed） |
| 手动启动 8014 后验证 | PASS，验证后进程已关闭（8014 当前无监听） |
| `--auto-server` 自启动复跑 | PASS，脚本自动拉起/关闭 8014，退出码 0（首次 + L1 修复后复验各一次） |
| 严重 / 中等缺陷 | 0 / 0；另有 5 条低风险/信息观察项（L1 已修复关闭，见 §5） |
| 真实高德模式 | 代码审查 + 现有 mock 单测覆盖（按任务要求不联网），14 条 live/amap 相关单测通过；L1 地铁/公交 `steps.type` 已修复并独立探针复验 |

## 1. 环境与命令

| 组件 | 版本 |
| --- | --- |
| Python | 3.12.10 |
| Node.js | v24.21.0 |
| FastAPI / uvicorn / requests / pytest | 0.162.0 / 0.54.0 / 2.34.2 / 9.2.0 |
| 高德 Key | 无（`AMAP_KEY` 未设置、`config.local.json` 不存在）→ 演示模式 |

实际执行命令（Windows PowerShell，工作目录 `C:\Users\26876\Desktop\supernav`）：

```powershell
# 1) 手动启动服务（后台）——只使用 8014，未占用 8000
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8014
Invoke-RestMethod http://127.0.0.1:8014/api/healthz        # {"status":"ok"}

# 2) 主验证（对运行中的服务做黑盒验证 + 独立复算 + 静态/源码审查 + 单测）
python verify/smoke.py
# 统计：PASS=1040  FAIL=0  WARN=0；退出码 0

# 3) 自动启动/关闭复跑（证明 smoke.py 可独立、可重复运行）
python verify/smoke.py --auto-server
# [server] 已自动启动 / 已关闭；统计：PASS=1040 FAIL=0；退出码 0

# 4) 补充证据
python -m pytest -q                    # 57 passed in 1.08s
python -m pytest -q -k "TestLiveMode or amap"   # 14 passed, 43 deselected
node --check web/app.js                # rc=0
```

L1 修复后的复验命令（2026-11-15 13:12）：

```powershell
python verify/smoke.py --auto-server                     # PASS=1040 FAIL=0 WARN=0，退出码 0（脚本自行起停 8014）
python -m pytest -q                                      # 57 passed in 1.05s
python -m pytest -q tests/test_backend.py::TestLiveMode::test_live_transit_parses_steps_and_fare   # 1 passed
node --check web/app.js                                  # rc=0
```

> 说明：验证开始时 8000 端口已有其他进程监听（非本验证启动），全程未使用 8000；验证结束后 8014 无监听进程。

## 2. 结果总览（1040 项检查分布）

| 分组 | PASS | FAIL |
| --- | ---: | ---: |
| 1. 健康 / 状态（`/api/healthz`、`/api/status`） | 17 | 0 |
| 2. 规划矩阵 2–5 段 + location + 10 点上限 | 720 | 0 |
| 3. 参数校验（4xx、错误格式） | 127 | 0 |
| 4. 计价参数 options | 84 | 0 |
| 5. 安全（Key 不泄漏 / `.gitignore`） | 26 | 0 |
| 6. 前端静态资源 | 19 | 0 |
| 7. 前端关键逻辑代码审查 + `node --check` | 44 | 0 |
| 8. 后端 pytest | 2 | 0 |
| 其他（服务可达） | 1 | 0 |
| **合计** | **1040** | **0** |

原始证据：`verify/artifacts/smoke-run.json`（逐条 PASS/FAIL/证据样本，复验后已覆盖为最新一次运行）、`verify/artifacts/smoke-console*.txt`、`verify/artifacts/server-8014*.log`；L1 复验专用证据：`verify/artifacts/l1-recheck.json`。
验证时代码快照 SHA256（16 位前缀）见 `verify/artifacts/source-sha256.json`（已更新为 L1 修复后的快照）。**L1 修复后快照**：`backend/amap.py e6c74c4452bed0c0`、`tests/test_backend.py 31ea771de4ce5318`、`backend/planner.py b1027ded506c1aa1`、`backend/costs.py 058d3d72d25ed32e`、`backend/demo.py 864d74fbd0317a16`、`backend/app.py 78edb847f21c1ca7`、`web/app.js 40f914c0f35076ef`、`web/index.html 5000b653267f9ce4`、`web/styles.css e5425297591c4c6a`。**首次验证快照（修复前）**：`backend/amap.py e2ce17f5dde4a1cf`、`tests/test_backend.py 259ac44e1b665239`；其余文件未变，两次快照差异仅这两个文件（与 Lead 声明的修复范围一致）。

## 3. 逐项验证结论

### 3.1 启动服务（PASS）

- `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8014` 启动成功，`GET /api/healthz` → `200 {"status": "ok"}`。
- 验证结束后手动进程与 auto-server 进程均已关闭：8014 端口连接数 0；8000 未占用/未触碰。

### 3.2 契约（PASS）

`GET /api/status`（实测响应节选）：

```json
{
  "ok": true,
  "version": "0.1.0",
  "demo": true,
  "demo_reason": "未配置 AMAP_KEY，使用演示数据",
  "defaults": {"fuel_price": 7.8, "fuel_consumption": 8.0, "transit_strategy": 0,
               "taxi_base_fare": 13.0, "taxi_base_km": 3.0, "taxi_per_km": 2.3},
  "modes": [{"value": "walking", "label": "步行"}, {"value": "bicycling", "label": "骑行"},
            {"value": "transit", "label": "公交/地铁"}, {"value": "taxi", "label": "打车"},
            {"value": "driving", "label": "驾车"}]
}
```

- 字段齐全、类型正确；`defaults` 6 个键与契约完全一致；`modes` 顺序/标签与契约一致；`demo_reason` 非空。
- `POST /api/plan` 演示模式 2–5 段混合方式全部成功（1/2/3/4/5 段）。5 段组合实测（北京南站→故宫→北京西站→国贸→中关村→首都机场，`[walking, bicycling, transit, taxi, driving]`）：

| 段 | 方式 | distance_m | duration_s | cost.total | 费用明细 |
| ---: | --- | ---: | ---: | ---: | --- |
| 0 | walking | 7029 | 5623 | 0.0 | （空） |
| 1 | bicycling | 8267 | 2480 | 0.0 | （空） |
| 2 | transit | 15948 | 3253 | 5.0 | transit_fare 5.0 公交/地铁票价 |
| 3 | taxi | 19384 | 1745 | 50.68 | taxi_fare 50.68 打车费 |
| 4 | driving | 31257 | 2813 | 26.26 | fuel 19.51 油费；toll 6.75 过路费 |

- 逐段字段：`index/mode/mode_label/from/to/distance_m/duration_s/cost/summary/steps/polyline/warnings` 全部存在且类型正确；`distance_m/duration_s` 为非负整数；`polyline` ≤500 点、经纬度合法（实测每段 25 点）；公交段 `steps` 非空且为「步行→线路→步行」。
- `totals`：距离 81885（=7029+8267+15948+19384+31257）、时长 15914、`cost.total` 81.94；明细按 key 合并（transit_fare 5.0 + taxi_fare 50.68 + fuel 19.51 + toll 6.75 = 81.94）。
- `mode == "demo"`、`generated_at` 为带时区 ISO 8601；演示横幅所需标志 `status.demo=true` + `demo_reason` + 每段 `mode` 均存在，前端 `#demo-banner`/`setDemoBanner` 逻辑见 3.6。
- 费用一致性：所有 `cost.total == sum(breakdown.amount)`（按分比较，误差 0）；所有金额均为 2 位小数；步行/骑行 `breakdown=[]` 且 `total=0`。

### 3.3 参数校验、location 直接坐标、10 点上限（PASS）

共 25 组非法输入均返回 `4xx`（业务错误 400，Pydantic 层错误被包装为 422），`error.code` 在契约枚举内、`error.message` 可读、`error.hint` 为字符串。典型：

| 用例 | HTTP | error.message |
| --- | ---: | --- |
| 仅 1 个点 | 400 | 至少需要起点和终点 |
| 11 个点 | 400 | 最多支持 10 个地点 |
| modes 数量不符 | 400 | modes 数量必须等于地点数减一（hint：2 个地点需要 1 个交通方式，当前 0 个） |
| 未知方式 `flight` | 400 | 不支持的交通方式：flight（hint 列出 5 种可用方式） |
| 空地址 | 400 | 第 1 个地点缺少地址 |
| location 小数位 >6 | 400 | 第 1 个地点 location 小数点后最多 6 位 |
| `transit_strategy=9` / `2.5` | 400 | options.transit_strategy 必须是 0–8 的整数 |
| `fuel_price=-1` / `"abc"` / `true` | 400 | options.fuel_price 必须在 0–1000 之间 / 必须是数字 |
| 非法 JSON body | 422 | 请求参数格式不正确（包装为统一 `error` 结构，无裸 FastAPI detail 漏出） |

- 其余覆盖：points 非数组 / 缺失、modes 缺失/过多、point 非对象、address 非字符串、location 非字符串/格式错/越界，均为 4xx 且 message 可读。
- `location` 直接坐标成功路径：`{"location":"116.378,39.865","name":"起点坐标"}` → 响应 `input` 原样回显、`name=起点坐标`、`location={"lng":116.378,"lat":39.865}`；地址+坐标混用（driving/taxi 两段）成功，坐标原样解析（`116.123456,39.123456`、`116.5,39.95` 均保留）。
- 10 点上限：10 点 9 段规划成功且逐段/合计校验全通过；11 点被拒绝（400）。

### 3.4 费用正确性（独立复算，PASS）

用**响应中的 `distance_m`** 与契约公式独立复算（脚本不 import 业务费用函数；金额按分比较，容差 0.01 元）：

| 用例 | 复算公式 | 复算值 | 接口值 | 结果 |
| --- | --- | --- | --- | --- |
| driving 段 31257m | 油费 = 31.257 km × 8.0/100 × 7.8 | 19.5044 → 19.50（明细 19.51，见观察项 L2） | fuel 19.51 | PASS（差 0.006） |
| driving 段 31257m | 过路费 = max(0, 31.257−20) × 0.6 | 6.7542 → 6.75 | toll 6.75 | PASS |
| driving 段 31257m | 总价 = round2(19.5044+6.7542) | 26.26 | total 26.26 | PASS |
| driving 自定义 8.5 元/L、6.7L/100km、26373m | 油费 = 26.373 × 6.7/100 × 8.5 | 15.0194 → 15.02 | fuel 15.02 | PASS |
| 同上 | 过路费 = max(0, 26.373−20) × 0.6 | 3.8238 → 3.82 | toll 3.82 | PASS |
| taxi 段 19384m | 13 + max(0, 19.384−3) × 2.3 | 50.6832 → 50.68 | taxi_fare 50.68 | PASS |
| transit 段 15948m | 阶梯：≤22km → 5 元 | 5.0 | transit_fare 5.0 | PASS |
| walking / bicycling（多段） | 0 元、空明细 | 0.0 / [] | 0.0 / [] | PASS |
| 合计 | 按 key 合并逐段明细；total=明细和 | 81.94 | 81.94 | PASS |

补充说明（避免误报/WARN）：

- **taxi 容差**：任务允许「内部未取整距离」带来 ≤1 元误差。演示模式 `backend/demo.py` 传给估算函数的是已取整的 `distance_m`（`backend/costs.py:62`），因此实测与「13+max(0,km−3)×2.3」严格一致（差 ≤0.01）。smoke 同时做了严格 0.011 与 ≤1 元两档判定，本次严格档通过、≤1 元档仅作兜底，未触发差异。
- **transit 阶梯**：`demo.route` 的公交段距离含 1 km 步行接驳（`+1000m`），票价按该 `distance_m` 计（`backend/demo.py:293`）。本次按任务要求用响应 `distance_m` 复算，15.948km 落在 ≤22km 档（5 元），一致；未把「直线×1.25」当距离而误报。
- **driving 分项残差**：见 §5-L2，单项可能因「先求和再四舍五入 + 保证明细和=total」产生 0.01 元级补偿，总价与公式一致。
- 顺带交叉验证演示模式速度/系数：walking 7029m/5623s≈4.5km/h、bicycling 8267m/2480s≈12km/h、taxi 19384m/1745s≈40km/h、driving 31257m/2813s≈40km/h；transit 等效 17.65km/h（含 5 分钟等待 + 1km 接驳），与契约第 6 节一致。

### 3.5 安全（PASS）

- `/api/status`、`/api/plan`、`/api/openapi.json`、`/`、`/app.js`、`/styles.css`、`/favicon.svg` 响应中均未出现 Key 值形态：32 位十六进制、`restapi.amap.com`、JSON 字段 `"amap_key":`、query `key=`（脚本逐项扫描）。
- `/api/status` 的 `demo_reason` 文本含环境变量名 `AMAP_KEY`（如「未配置 AMAP_KEY，使用演示数据」）——这是**演示原因说明**、契约明示的文案，不是 Key 泄漏；已验证同时不存在 Key 值形态。
- 前端源码 `web/index.html`、`web/app.js`、`web/styles.css` 不含 `AMAP_KEY` / `amap_key` / `restapi.amap.com`；前端无硬编码 32 位 Key。
- `backend/*.py` 无硬编码 32 位 Key；Key 仅从环境变量 `AMAP_KEY` 或 `config.local.json` 读取（`backend/config.py:67`）。
- `.gitignore` 第 10 行包含 `config.local.json`、并包含 `.env`；`git check-ignore -v config.local.json` 命中 `.gitignore:10`；工作区不存在 `config.local.json`；`config.local.example.json` 的 `amap_key` 为占位符（含「你的」字样）。
- `/api/status` 响应键恰好为契约 6 个字段（无额外实现细节/配置泄漏）；静态路径目录穿越 `..%2f..%2f` 返回 404（不接受）。
- 未执行任何 git 写操作（无 add/commit/checkout）；未提交任何 Key。

### 3.6 前端静态资源与关键逻辑（PASS，代码审查 + node --check）

- `GET /` → 200 `text/html`，标题 `SuperNav · 交通规划`，含 `#demo-banner`、三主题按钮、同源 `styles.css`/`app.js`；`/styles.css` → 200 `text/css`；`/app.js` → 200 JavaScript；`/favicon.svg` → 200 SVG；`/web/app.js` 兼容路径 200；未知路径 404 且为统一 `error` JSON。
- `node --check web/app.js` → rc 0；`index.html` 内联主题引导脚本抽取后 `node --check` 也 rc 0。
- 三主题（明/灰/暗）：`styles.css` 有 `:root`（明）、`[data-theme="gray"]`、`[data-theme="dark"]`（含 `color-scheme: dark`）三套变量；`index.html` 有 3 个 `data-theme-choice` 按钮 + 首屏内联脚本读取 `localStorage` 键 `supernav-theme` 防止闪烁；`app.js` 中 `THEME_KEY='supernav-theme'`、`safeGetItem/safeSetItem`、`applyTheme()` 写 `data-theme` 并更新 `aria-pressed`，点击持久化。
- 途经点：`#add-point`「＋ 添加途经点」调用 `addWaypoint()`（在终点前插入点与段、`MAX_POINTS=10` 时禁用按钮）；每个途经点有「删除」按钮（`data-remove-index` → `removeWaypoint()`，边界检查 `index<=0 || index>=points.length-1`）；`state.points`/`state.modes` 同步 splice，段标签与方式下拉随表单重绘。
- 演示横幅：`#demo-banner` + `#demo-banner-reason`，`applyStatus()` 依 `status.demo`/`demo_reason` 展示；结果区 `updateSourceBadge` 依 `mode==="demo"` 显示「演示数据」并再次确保横幅可见。
- 错误展示：`#alert` 区域展示 `error.message` + `error.hint`（兼容 FastAPI `detail`/数组与统一 `error` 包装）、请求超时（AbortController 45s）、断网/后端未启动提示、`/api/status` 失败可「重试连接」；提交失败会隐藏旧结果并聚焦错误区。
- 请求与校验：`fetch('/api/status')`、`fetch('/api/plan', POST)`；提交前校验 2–10 点、空地址、modes 数量与合法性；payload 为 `{points:[{address}],modes,options}`，options 仅发送页面设置的 fuel_price/fuel_consumption/transit_strategy。
- 无外部 CDN：`index.html` 无外链 `src/href`，`app.js` 唯一 `http://` 为 SVG 命名空间，CSS 无 `@import`/外链 `url()`。

### 3.7 真实高德模式：代码审查 + 现有 mock 单测（PASS，按任务不联网）

代码审查要点（`backend/amap.py`、`backend/planner.py`）：

- 接口与契约一致：geocode `/v3/geocode/geo`；driving `/v5/direction/driving`（`show_fields=cost,navi,polyline`、`strategy=32`）；walking/bicycling `/v5/direction/{walking,bicycling}`；transit `/v5/direction/transit/integrated`（`strategy` 来自 options 0–8，city1/city2 取 geocode citycode）。
- 解析容错：`as_int/as_float_opt/as_text` 兼容字符串/数字/缺失；driving 的 `duration` 兼容 `paths[0].duration` 与 `cost.duration`；`route.taxi_cost` 优先、`cost.taxi_cost` 兜底；过路费 `paths[0].cost.tolls`；transit 票价 `cost.transit_fee` → `segments[].cost` → `buslines[].ticket_price` 兜底，缺失时按 0 元 + warning「未返回公交票价，按 0 元计」；打车费缺失时按估算 + warning「打车费为估算值」；结构缺失降级为空路线并加 warning，不崩溃。
- 错误与安全：`status!="1"` → `AMAP_ERROR` 并带 `info/infocode`，Key 类 infocode 给出明确 hint；`RequestException`/HTTPError 消息经 `redact()` 抹掉 Key；超时 10s；未配置 Key 抛 `KEY_MISSING`；统一 500 不输出异常细节。
- 真实编排：`planner.resolve_points_live` 对同一地址地理编码去重；`location` 直接坐标跳过地理编码；`citycode` 供公交规划；响应 `mode="live"`。
- 公交步骤类型：`_transit_steps` 现按 busline `type`（含「地铁」）或线路名以「地铁」开头区分 `subway`/`bus`（L1 修复，复验见 §3.8），前端 `stepTypeLabel` 支持 `subway → 地铁`。
- Key 读取优先级：环境变量 > `config.local.json`；`DEMO_MODE=1` 即使有 Key 也强制演示（单测覆盖）。
- 现有离线单测证据：`tests/test_backend.py` 的 `TestLiveMode`（mock `requests.Session.get`，覆盖 live taxi/driving/transit 解析、字符串/数字混用、缺失字段降级、geocode 去重、location 跳过 geocode、AMAP_ERROR/GEOCODE_FAILED/KEY_MISSING 映射）等共 **14 条 live/amap 相关用例通过**；全量 `pytest -q` → **57 passed**。未发任何真实高德请求。

### 3.8 L1 修复复验（Lead 修复后，PASS）

Lead 声明的修复范围与实际文件差异一致（仅两处文件变化）：

1. `backend/amap.py` `_transit_steps` 的 bus 分支：根据 busline `type`（含「地铁」）或线路名以「地铁」开头，把 `steps[].type` 置为 `"subway"`，否则 `"bus"`。
2. `tests/test_backend.py`：`TRANSIT_PAYLOAD` 的地铁线路补充 `"type": "地铁线路"`，断言改为 `["walking", "subway", "walking"]`。

复验证据（2026-11-15 13:12）：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量 smoke | `python verify/smoke.py --auto-server` | **1040 PASS / 0 FAIL / 0 WARN，退出码 0**；脚本自行拉起并关闭 8014 |
| 全量单测 | `python -m pytest -q` | **57 passed** |
| L1 定向单测 | `python -m pytest -q tests/test_backend.py::TestLiveMode::test_live_transit_parses_steps_and_fare` | **1 passed**（断言 `["walking","subway","walking"]`） |
| 前端语法 | `node --check web/app.js` | rc=0 |

独立解析探针（verifier 直接调用 `AmapClient._parse_transit_route`，构造 mock 高德响应，**不联网、不依赖单测断言**）：

| 探针输入（buslines[0]） | 解析出的 step types | 判定 |
| --- | --- | --- |
| `name="地铁4号线"` + `type="地铁线路"` | `["walking", "subway", "walking"]` | PASS |
| `name="地铁2号线"`、无 type（线路名兜底） | `["walking", "subway", "walking"]` | PASS |
| `name="公交300路"` + `type="公交线路"` | `["walking", "bus", "walking"]` | PASS（不应误判 subway） |
| `name="公交300路"`、无 type | `["walking", "bus", "walking"]` | PASS |

- 前端 `web/app.js` 的 `stepTypeLabel` 已含 `subway: '地铁'`，修复后真实模式地铁步骤会显示「地铁」，公交仍显示「公交」。
- 结论：**L1 已修复并复验通过，无回归**（smoke 1040/0、pytest 57 passed）。原始证据：`verify/artifacts/l1-recheck.json`。

## 4. 工作区完整性

- verifier 只写 `verify/**`（`verify/smoke.py`、`verify/report.md`、`verify/artifacts/`），**未修改** `backend/`、`web/`、`tests/`、`docs/`、`README.md`、`.gitignore`、`requirements.txt`、`run.ps1`；未执行 git 写操作。
- 首次验证期间其他成员并行改动了 `README.md`（`git status` 显示 `M README.md`）并新增 `docs/screenshot-*.png`；这些不是 verifier 的改动。
- 首次验证后，Lead 按观察项 L1 修复了 `backend/amap.py` 与 `tests/test_backend.py`（verifier 未参与修改）。复验前后快照对比确认**仅这两个文件变化**：`backend/amap.py e2ce17f5dde4a1cf → e6c74c4452bed0c0`、`tests/test_backend.py 259ac44e1b665239 → 31ea771de4ce5318`，其余文件哈希不变。复验已针对该新快照重跑 smoke 与 pytest 并更新本报告（见 §3.8）。

## 5. 发现的问题（按严重度分级）

**严重（阻塞验收）：无。**
**中等：无。**

低风险 / 观察项（不阻塞本 MVP 验收，建议后续迭代处理）：

| 编号 | 级别 | 描述 | 位置 / 复现建议 |
| --- | --- | --- | --- |
| L1 | **已修复（复验 PASS）** | 原问题：真实模式公交 steps 的 `type` 对地铁线路也统一标为 `bus`（前端标签显示「公交」）。Lead 已按 busline `type`（含「地铁」）或线路名以「地铁」开头区分 `subway`/`bus`，前端 `subway` 显示「地铁」。verifier 复验：smoke 1040/0、pytest 57 passed（含 subway 断言）、独立解析探针 4 例（地铁→subway、公交→bus）全部通过，无回归 | `backend/amap.py:316`（修复后 line 321-323）；`tests/test_backend.py:210`；证据 §3.8 与 `verify/artifacts/l1-recheck.json` |
| L2 | 低（金额 1 分补偿） | driving 明细按「先求和再四舍五入 + 保证 sum(breakdown)==total」设计，当单项四舍五入与总价差 1 分时会把残差补到最大项：如 31257m 精确油费 19.5044→单项应为 19.50，接口 fuel=19.51（total 26.26 正确）。符合契约「先求和再四舍五入、明细和=total」，验收应以 total 与复算总价为准 | `backend/costs.py:47-58` `build_cost`；复现：`POST /api/plan`（北京南站→首都机场，driving） |
| L3 | 信息（容差说明） | 演示模式 taxi 使用取整后的 `distance_m` 估算，实测与公式严格一致；任务提到的 ≤1 元误差只在实现使用「未取整内部距离」时出现。smoke 已加 ≤1 元兜底判定，避免未来实现的合理误差被误报 | `backend/demo.py:288`、`backend/costs.py:62` |
| L4 | 信息（范围取舍） | 前端未提供「直接经纬度」输入 UI（仅地址输入），而后端/契约支持 `location`；MVP 无此要求，属正常范围取舍 | `web/app.js` `buildPayload()` |
| L5 | 信息（验证脚本自纠） | smoke 初版曾把 `demo_reason` 中的环境变量名 `AMAP_KEY` 误判为 Key 泄漏（1 条 FAIL）；已改为只扫描 Key 值形态（32 位十六进制/高德域名/`amap_key` 字段/`key=`），复跑 PASS。记录以免误读为产品缺陷 | `verify/smoke.py` `check_security` |
| L6 | 信息（产物管理） | `verify/artifacts/` 被 `.gitignore` 忽略（运行证据不入库）；`verify/smoke.py` 与 `verify/report.md` 可提交。若 Lead 希望留存证据，需另行决定是否调整 `.gitignore` | `.gitignore:16` |

## 6. 复现 / 复验方式

```powershell
# A. 自动模式（推荐，脚本自己起停 8014，可重复运行）
python verify/smoke.py --auto-server
# 期望最后两行：
# 统计：PASS=1040  FAIL=0  WARN=0
# 结论：PASS（全部检查通过）

# B. 手动模式（对指定服务做黑盒验证；不占用 8000）
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8014
python verify/smoke.py            # 或 $env:BASE_URL="http://127.0.0.1:8014"; python verify/smoke.py

# C. 离线单测
python -m pytest -q               # 57 passed

# D. 前端语法
node --check web/app.js
```

若 Lead 再次修复/改动业务代码，verifier 可重新执行 A 并更新本报告与 `verify/artifacts/smoke-run.json`；本次 L1 修复后的复验即按此流程完成（见 §3.8，复验后仍为 1040 项检查、0 FAIL）。

## 7. 验证局限

- 无真实高德 Key、按任务不联网：真实模式仅做代码审查 + 现有 mock 单测，未做真实高德联调/配额/错误码实测。
- 无浏览器/无自动化 UI 驱动：前端为静态资源可用性 + `node --check` + 关键逻辑代码审查，未做视觉渲染、真实点击/主题切换截图比对、移动端布局与跨浏览器验证（`docs/screenshot-*.png` 由前端成员提供，未纳入本次自动断言）。
- 未做性能/并发/长跑测试；未做无障碍（a11y）自动化审计（仅代码层面确认基础 aria 属性存在）。
- 验证结论对应 `verify/artifacts/source-sha256.json` 记录的文件快照（首次验证快照 + L1 修复后复验快照，见 §2/§4）；并行改动（README/截图）与 Lead 的 L1 修复均已在本报告 §4 说明。
