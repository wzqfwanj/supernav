# SuperNav 交通规划 MVP — 接口契约（v0.1）

> 本文件是后端、前端、验证三方共同遵守的契约。实现不得随意更改字段名与语义；若确需调整，先通知 Lead。

## 1. 目标

一个「交通规划」最小可用功能：用户输入多个地点（起点、途经点、终点），为每段选择一种交通方式，系统逐段规划并计算**路费**，最后给出逐段明细与全程合计。

交通方式（mode）取值固定为：

| 值 | 含义 | 说明 |
| --- | --- | --- |
| `walking` | 步行 | 高德步行路径规划 |
| `bicycling` | 骑行 | 高德骑行路径规划 |
| `transit` | 公交/地铁 | 高德公交路径规划（含步行接驳） |
| `taxi` | 打车 | 高德驾车路线 + 高德预估打车费用 |
| `driving` | 驾车 | 高德驾车路线，费用 = 油费 + 过路费 |

## 2. 数据流

浏览器（`web/`）→ 本服务 FastAPI（`backend/`）→ 高德 Web 服务 API（`restapi.amap.com`）。
**高德 Key 只保存在后端**（环境变量或本地配置文件），绝不下发到前端。

无 Key 时进入**演示模式**：返回结构相同的模拟路线/费用，前端顶部明确标注「演示模式」。
有 Key 时使用真实高德数据。

## 3. 接口

### 3.1 `GET /api/status`

健康状态与运行配置，前端启动时调用。

```json
{
  "ok": true,
  "version": "0.1.0",
  "demo": true,
  "demo_reason": "未配置 AMAP_KEY，使用演示数据",
  "defaults": {
    "fuel_price": 7.8,
    "fuel_consumption": 8.0,
    "transit_strategy": 0,
    "taxi_base_fare": 13.0,
    "taxi_base_km": 3.0,
    "taxi_per_km": 2.3
  },
  "modes": [
    {"value": "walking", "label": "步行"},
    {"value": "bicycling", "label": "骑行"},
    {"value": "transit", "label": "公交/地铁"},
    {"value": "taxi", "label": "打车"},
    {"value": "driving", "label": "驾车"}
  ]
}
```

### 3.2 `GET /api/healthz`

`{"status": "ok"}`，仅用于探活。

### 3.3 `POST /api/plan`

请求体：

```json
{
  "points": [
    {"address": "北京南站"},
    {"address": "故宫博物院"},
    {"address": "北京西站"}
  ],
  "modes": ["transit", "walking"],
  "options": {
    "fuel_price": 7.8,
    "fuel_consumption": 8.0,
    "transit_strategy": 0
  }
}
```

- `points`：2–10 个地点，按行程顺序。每个点必须是：
  - `{"address": "地址文本"}`（后端调用高德地理编码解析），或
  - `{"location": "116.378,39.865", "name": "可选名称"}`（直接给经纬度，`lng,lat`，小数点后最多 6 位）。
- `modes`：长度必须等于 `points.length - 1`，取值见上表。
- `options`：全部可选，缺省用 `/api/status` 的 `defaults`。`transit_strategy` 取值 0–8（高德公交换乘策略，默认 0 推荐）。

成功响应 `200`：

```json
{
  "mode": "live",
  "generated_at": "2026-11-15T12:00:00+08:00",
  "points": [
    {
      "input": "北京南站",
      "name": "北京南站",
      "formatted_address": "北京市丰台区北京南站",
      "city": "北京市",
      "citycode": "010",
      "location": {"lng": 116.378, "lat": 39.865}
    }
  ],
  "legs": [
    {
      "index": 0,
      "mode": "transit",
      "mode_label": "公交/地铁",
      "from": {"name": "北京南站", "location": {"lng": 116.378, "lat": 39.865}},
      "to": {"name": "故宫博物院", "location": {"lng": 116.397, "lat": 39.918}},
      "distance_m": 12345,
      "duration_s": 1800,
      "cost": {
        "total": 5.0,
        "breakdown": [
          {"key": "transit_fare", "label": "公交/地铁票价", "amount": 5.0}
        ]
      },
      "summary": "地铁4号线 → 地铁2号线",
      "steps": [
        {"type": "walking", "text": "步行 300 米", "distance_m": 300, "duration_s": 240}
      ],
      "polyline": [[116.378, 39.865], [116.39, 39.87]],
      "warnings": []
    }
  ],
  "totals": {
    "distance_m": 12345,
    "duration_s": 1800,
    "cost": {
      "total": 5.0,
      "breakdown": [
        {"key": "transit_fare", "label": "公交/地铁票价", "amount": 5.0}
      ]
    }
  }
}
```

字段约束：

- `mode`：`"live" | "demo"`，表示本次结果数据来源。
- `points[].input` 原样回显；`name` 为高德格式化地址或直接给出的名称；`location.lng/lat` 为数字；`citycode` 供公交规划使用，无法解析时为空字符串。
- `legs[].distance_m`、`duration_s` 为非负整数（米/秒）。
- `legs[].cost.breakdown` 为费用明细，可能为空数组（步行/骑行免费时 `total=0`、`breakdown=[]`）。所有 `amount` 保留 2 位小数，且 `sum(amount) == total`（允许 0.01 元内浮点误差，后端需先求和再四舍五入）。
- `legs[].summary` 为可读路线摘要，可能为空字符串。
- `legs[].steps` 为简化步骤（公交方案必须给出「步行/公交线路/地铁/火车」顺序摘要；其他方式可为空数组）。
- `legs[].polyline` 为 `[lng, lat]` 数组，用于前端绘制示意路线；最多 500 个点，可能为空数组。
- `legs[].warnings` 为字符串数组，例如「未返回公交票价，按 0 元计」。
- `totals` 为各段求和；`totals.cost` 的费用明细按 `key` 合并、金额累加后四舍五入。
- `generated_at` 为 ISO 8601 带时区时间。

错误响应：非 2xx 时返回

```json
{"error": {"code": "INVALID_INPUT", "message": "至少需要起点和终点", "hint": ""}}
```

`code` 取值：`INVALID_INPUT`、`GEOCODE_FAILED`、`AMAP_ERROR`、`KEY_MISSING`、`INTERNAL_ERROR`。
FastAPI 自动产生的 422 校验错误可不包装，但前端必须能兜底展示。

## 4. 费用模型（最小实现）

| 方式 | 费用计算 | 费用明细 key / label |
| --- | --- | --- |
| `walking` | 0 | 无（空 breakdown） |
| `bicycling` | 0（不估算共享单车租金） | 无 |
| `transit` | 高德公交规划返回的票价 `transit_fee` | `transit_fare` / `公交/地铁票价` |
| `taxi` | 高德驾车路线返回的 `route.taxi_cost`（预计出租车费用） | `taxi_fare` / `打车费` |
| `driving` | 油费 = 距离(km) × 油耗(L/100km) ÷ 100 × 油价(元/L)；过路费 = 高德返回 `tolls` | `fuel` / `油费`；`toll` / `过路费` |

补充规则：

- 打车费用缺失时，按 `起步价 13 元（含 3 公里）+ 超出部分 2.3 元/公里` 估算，并加 warning「打车费为估算值」。
- 驾车过路费缺失按 0 计，仅在有值且大于 0 时加入明细。
- 油费为 0 时不加入明细。
- 公交/地铁票价缺失时按 0 计并加 warning。
- 金额四舍五入到 0.01 元；展示由前端格式化。

## 5. 高德接口与解析（后端实现参考）

- 地理编码：`GET https://restapi.amap.com/v3/geocode/geo?key=...&address=...`，取 `geocodes[0]` 的 `location`、`formatted_address`、`city`、`citycode`、`province`、`district`。
- 驾车：`GET https://restapi.amap.com/v5/direction/driving?origin=...&destination=...&show_fields=cost,navi,polyline&strategy=32`
  - `route.taxi_cost`：预计打车费；`route.paths[0].distance`；`duration` 可能在 `paths[0].duration` 或 `paths[0].cost.duration`；过路费在 `paths[0].cost.tolls`；折线在 `paths[0].steps[].polyline`。
- 步行：`/v5/direction/walking`，`paths[0].distance/duration`，折线在 `steps[].polyline`。
- 骑行：`/v5/direction/bicycling`，同上。
- 公交：`GET https://restapi.amap.com/v5/direction/transit/integrated?origin=...&destination=...&city1=...&city2=...&strategy=0&show_fields=cost,navi,polyline`
  - `route.transits[0].distance`；耗时在 `duration` 或 `cost.duration`；票价在 `cost.transit_fee`（若缺失，再尝试 `segments[].cost`、`buslines[].ticket_price`）；`segments[]` 内可能含 `walking`、`bus`、`railway`，按顺序生成 steps/summary。
  - 解析必须**容错**：字段可能是字符串或数字；结构缺失时降级而不是崩溃。
- Amap 返回 `status != "1"` 视为错误，带上 `info`、`infocode`；Key 错误（infocode=10001/10009 等）返回 `AMAP_ERROR` 并在 message 中说明 Key 问题。
- 请求超时 10 秒；同一请求内对相同地址去重地理编码。

## 6. 演示模式

- 触发条件：未配置 `AMAP_KEY`（默认 auto），或显式 `DEMO_MODE=1`。返回 `mode="demo"`，费用/路线为确定性模拟数据。
- 覆盖内置示例地点（如 北京南站、故宫博物院、北京西站、国贸、中关村、天安门、王府井、三里屯、首都机场、北京大学），也接受任意地址（按稳定哈希生成北京周边坐标）。
- 模拟规则（服务端实现）：
  - 两点间直线距离 × 路线系数（步行 1.15、骑行 1.2、驾车 1.3、公交 1.25）为段距离；
  - 速度：步行 4.5 km/h、骑行 12 km/h、驾车 40 km/h、公交 25 km/h；公交额外等待 5 分钟、含 1 公里步行接驳；
  - 公交票价按距离阶梯（≤6km 3 元，≤12km 4 元，≤22km 5 元，≤32km 6 元，更远每 20km +1 元）；
  - 打车费按统一估算公式；驾车过路费 `max(0, 里程-20) × 0.6` 元；油费按实际距离与配置计算；
  - 折线生成近似直线（可带轻微弧度），供示意展示。
- 演示模式不得伪装成真实数据：前端必须展示「演示模式」横幅，`mode` 字段必须为 `"demo"`。

## 7. 目录与分工（仅用于并行开发，勿越界）

| 目录/文件 | 负责人 | 内容 |
| --- | --- | --- |
| `backend/`、`tests/test_backend.py` | backend-dev | FastAPI 应用、高德客户端、费用计算、演示数据、后端单测 |
| `web/` | frontend-dev | `index.html`、`styles.css`、`app.js`（无框架、无 CDN） |
| `verify/` | verifier | 独立验证脚本与报告，不改动业务代码 |
| 根目录文档、`README.md`、`requirements.txt`、`.gitignore`、git | lead | 契约、脚手架、集成、提交 |

## 8. 运行方式

```powershell
# 安装依赖
python -m pip install -r requirements.txt

# 可选：配置高德 Key（二选一）
$env:AMAP_KEY = "你的高德 Web 服务 Key"
# 或在项目根目录创建 config.local.json：{"amap_key": "..."}（该文件被 .gitignore 忽略）

# 启动（项目根目录执行）
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000
```

## 9. 验收标准（MVP）

- `GET /api/status`、`GET /api/healthz` 正常；前端通过同源接口访问，无跨域配置。
- 无 Key（演示模式）下，2–5 段任意组合规划可成功返回，合计 = 各段之和，费用明细正确。
- 每段 5 种方式均可规划（真实 Key 存在时走真实接口；无 Key 时走模拟）。
- 输入非法（缺点、模式数量不符、未知模式、地址无法解析）返回 4xx 且 message 可读。
- 演示模式横幅可见；高德 Key 不出现在任何前端响应或源码中。
- 明 / 灰 / 暗 三种主题可切换并持久化（localStorage）。
- 前端无外部 CDN 依赖，后端测试可离线运行（pytest）。
- 后端单测覆盖费用计算、演示规划、参数校验；验证角色独立复核合计与契约。
