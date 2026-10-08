# sing-box 可视化管理面板（单用户轻量版）

一个人的 sing-box 管理面板：探针式实时监控大屏 + 入站管理 + 订阅生成 + 进程控制。

## 功能

- **📊 监控大屏**（哪吒探针风格）：实时上/下行速率曲线（ECharts，每秒刷新，保留 60 秒）、当前值 + 峰值、总流量、按入站流量统计、CPU/内存/硬盘/负载、入站状态卡片（在线/端口/协议/连接数）
- **🔌 入站管理**：shadowsocks / vmess / vless / trojan / hysteria2 的增删改、启用/停用；vless/trojan 一键 reality 配置（自动生成 keypair + short-id）；hysteria2 自动生成自签名证书
- **👤 极简用户模型**：每个入站自带一个主凭证（自动生成），最多再加 2 个备用凭证，可一键重新生成
- **🔗 订阅**：每个入站一个订阅链接 + 本地二维码（绝不调用第三方 QR API）；聚合订阅 `/sub`（纯文本）
- **⚙️ 进程控制**：启动/停止/重启 sing-box、实时日志、配置校验（每次写配置自动跑 `sing-box check`）
- **🔐 登录**：admin 单密码，默认 `admin`，首次登录强制修改

## 快速启动

```bash
cd singbox-panel
docker compose up -d --build
```

浏览器打开 `http://服务器IP:8080`，默认密码 `admin`。

> 注意：docker-compose.yml 里按需放行你实际使用的入站端口（取消对应注释）。

## 流量统计实现

sing-box 官方 release 二进制**不包含** v2ray_api（需自行编译），因此本面板采用 clash_api 方案：

- 后台每 2 秒轮询 `experimental.clash_api` 的 `/traffic`（总量速率）与 `/connections`
- 按 `metadata.type = "协议/入站tag"` 把每条连接的流量归因到入站，增量持久化到 sqlite
- 实时速率曲线保留最近 60 秒，峰值可一键清零

## 目录结构

```
singbox-panel/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── backend/
│   ├── main.py          # FastAPI 入口 + 静态前端托管
│   ├── db.py            # sqlite 数据层
│   ├── singbox.py       # 二进制管理（官方 releases 下载）
│   ├── config_mgr.py    # config.json 生成 + sing-box check 校验
│   ├── inbounds.py      # 入站 CRUD + 凭证管理
│   ├── subscribe.py     # 订阅链接 + 本地二维码
│   ├── process.py       # 进程 start/stop/restart/logs
│   ├── stats.py         # 流量轮询 + 速率曲线
│   └── requirements.txt
├── frontend/
│   └── index.html       # 单文件：Vue3 + Tailwind + ECharts（CDN）
└── data/                # volume 持久化：panel.db / config.json / sing-box 二进制 / 日志
```

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/login | 登录 |
| POST | /api/change-password | 改密码 |
| GET | /api/dashboard | 探针大屏数据 |
| GET/POST | /api/inbounds | 入站列表/新建 |
| PUT/DELETE | /api/inbounds/{id} | 修改/删除 |
| POST | /api/inbounds/{id}/regen-credential | 重生成主凭证 |
| POST/DELETE | /api/inbounds/{id}/backups | 备用凭证增删 |
| GET | /api/inbounds/{id}/link | 订阅链接+二维码 |
| GET | /sub | 聚合订阅（纯文本） |
| POST | /api/singbox/{start,stop,restart} | 进程控制 |
| GET | /api/singbox/logs | 日志 |
| POST | /api/singbox/check | 配置校验 |

除 `/api/login` 与 `/sub` 外，所有 `/api/*` 需要 `Authorization: Bearer <token>`。
