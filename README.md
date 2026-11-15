# SuperNav · 交通规划

一个极简的多段交通规划小应用：输入起点、途经点、终点，为每段选择交通方式，自动规划路线并计算**路费**（公交票价 / 打车费 / 过路费 / 油费），给出逐段明细与全程合计。

> 当前版本：MVP v0.1（仅实现「交通规划」这一最小功能）。

## 功能

- 多段行程组合：步行、骑行、公交/地铁、打车、驾车可任意搭配。
- 逐段费用与全程合计：公交票价、打车费、过路费、油费分别展示。
- 明 / 灰 / 暗三种主题，记忆用户选择。
- 高德 Key 未配置时自动进入**演示模式**（模拟数据，界面明确标注），配置后使用真实高德路线。
- 纯 HTML/CSS/JS 前端 + Python(FastAPI) 后端，无前端构建步骤、无外部 CDN。

## 界面预览

演示模式（明主题）：

![SuperNav 交通规划（明主题）](docs/screenshot-light.png)

暗主题：

![SuperNav 交通规划（暗主题）](docs/screenshot-dark.png)

## 快速开始

要求：Python 3.10+。

```powershell
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 启动（项目根目录）
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
# 或 Windows：.\run.ps1

# 3. 浏览器打开
# http://127.0.0.1:8000
```

未配置高德 Key 时即可体验：页面会显示「演示模式」，使用模拟路线与费用。

## 配置高德 Key（切换真实数据）

1. 到 [高德开放平台控制台](https://console.amap.com/dev/key/app) 创建应用，添加 **Web 服务** 类型的 Key。
2. 配置任选其一，二选一即可：
   - 环境变量（推荐）：
     ```powershell
     $env:AMAP_KEY = "你的高德 Web 服务 Key"
     ```
   - 项目根目录新建 `config.local.json`：
     ```json
     {"amap_key": "你的高德 Web 服务 Key"}
     ```
3. 重启服务，页面上的「演示模式」横幅消失即表示已接入真实数据。

> Key 只保存在后端，不会出现在前端代码或响应中。`config.local.json` 已被 `.gitignore` 忽略。

## 计价口径（MVP）

| 方式 | 费用 |
| --- | --- |
| 步行 / 骑行 | 免费（不估算共享单车租金） |
| 公交 / 地铁 | 高德公交规划返回的票价 |
| 打车 | 高德驾车路线返回的预估出租车费用；缺失时按「13 元含 3 公里 + 2.3 元/公里」估算 |
| 驾车 | 油费（里程 × 油耗 × 油价）+ 高德返回的过路费 |

油价、百公里油耗、公交换乘策略可在页面「计价设置」中调整。费用为规划估算值，实际以运营方为准。

## 目录结构

```
backend/          FastAPI 应用、高德客户端、费用计算、演示数据
web/              前端页面（index.html / styles.css / app.js）
docs/             接口契约、界面截图
tests/            后端单元测试
verify/           独立验证脚本与报告（非业务代码）
scripts/          Lead 验收脚本（acceptance.py）
run.ps1           本地启动脚本
config.local.example.json  本地 Key 配置示例
```

接口契约见 [docs/API-CONTRACT.md](docs/API-CONTRACT.md)。

## 测试与验收

```powershell
# 后端单元测试（离线，mock 高德接口）
python -m pytest -q

# 启动服务后做黑盒验收（演示模式）
python scripts/acceptance.py

# 独立验证（verifier 输出，含验证报告）
python verify/smoke.py
```

## 当前状态

- 演示模式后端单测、黑盒验收、独立验证已通过；页面已用 headless Chrome 实际渲染截图确认（明/暗主题）。
- **真实高德数据尚未联调**：当前环境没有高德 Key。按上文配置 Key 后重启服务即可切换真实路线；如真实 v5 响应字段与文档有出入，可能需要小幅调整解析（已做多层容错）。
- 在线部署、GitHub 发布按开发规则**待确认后**再执行。

## 后续（按开发规则，须先与用户确认）

- 在线部署（是否加访问账号密码）——待确认，尚未执行。
- 上传 GitHub 并发布（是否开源）——待确认，尚未执行。
- 真机/真实 Key 联调、更多规划能力（多方案对比、地图底图、高德 MCP 专属地图等）——后续迭代。

## 技术说明

- 路线规划使用高德 **Web 服务 API**（路径规划 2.0）：`/v5/direction/driving`、`/walking`、`/bicycling`、`/transit/integrated`，地理编码 `/v3/geocode/geo`。与高德 MCP Server 的路径规划能力同源，但直接 HTTP 调用更适合本应用的后端代理与费用计算。
- 本仓库由 Agent Team 并行实现，Lead 集成并验收。
