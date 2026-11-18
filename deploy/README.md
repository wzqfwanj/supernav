# 部署说明（dev 主机 + 阿里云 Caddy）

SuperNav 的线上地址：**https://wkeeli.cn/supernav/**（公开，无访问密码；无高德 Key 时为演示模式）。

## 拓扑

```
浏览器
  │ https://wkeeli.cn/supernav/
  ▼
阿里云 8.133.209.25 ── Caddy（自动 HTTPS）
  │ reverse_proxy http://180.76.180.249:8091
  ▼
dev 主机 180.76.180.249 ── systemd: supernav
  └─ /opt/supernav/.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8091
```

- 应用目录：`/opt/supernav`
- 服务名：`supernav.service`
- 端口：`8091`（仅对 Caddy 上游暴露）
- 子路径：Caddy `handle_path /supernav/*` 去掉前缀后转发；前端静态资源与 `api/*` 均使用相对路径，因此可在子路径下工作。

## 首次部署 / 更新

```bash
# 本地（项目根目录）：把当前提交打包
git archive --format=tar.gz -o supernav.tgz HEAD

# 上传到 dev 主机（本机的 ~/.ssh/config 已配置 ssh dev）
scp supernav.tgz dev:/tmp/supernav.tgz

# dev 主机上解包并安装依赖（保留已有 config.local.json）
ssh dev
rm -rf /opt/supernav
mkdir -p /opt/supernav
tar -xzf /tmp/supernav.tgz -C /opt/supernav
cd /opt/supernav
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 安装/更新 systemd 服务
cp deploy/supernav.service /etc/systemd/system/supernav.service
systemctl daemon-reload
systemctl enable --now supernav
systemctl restart supernav
```

## 阿里云 Caddy（只做一次）

```bash
# 备份
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-supernav-$(date +%Y%m%d-%H%M%S)

# 把 deploy/Caddyfile.snippet 的内容插入 wkeeli.cn { ... } 块内、
# 兜底 reverse_proxy localhost:5000 之前（注意用 Tab 缩进）
vi /etc/caddy/Caddyfile

caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## 验证

```bash
curl -sI https://wkeeli.cn/supernav/                      # 200 text/html
curl -s  https://wkeeli.cn/supernav/api/status            # demo=true
curl -s -X POST https://wkeeli.cn/supernav/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"points":[{"address":"北京南站"},{"address":"故宫博物院"}],"modes":["transit"]}'
```

## 真实高德数据（可选）

线上默认演示模式。配置真实数据二选一，然后 `systemctl restart supernav`：

- `/opt/supernav/config.local.json`：`{"amap_key": "..."}`（不要提交到 Git）
- 或 systemd 单元中 `Environment=AMAP_KEY=...`

配置后 `curl -s https://wkeeli.cn/supernav/api/status` 中 `demo` 应为 `false`。

## 已知说明

- 应用无登录、无密码，公开只读（规划接口会调用高德 API，若配置了真实 Key 请注意配额）。
- 线上 Caddy 已配置 CSP/安全响应头，前端无外部 CDN，控制台无外部依赖。
