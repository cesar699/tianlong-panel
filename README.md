# sing-box 可视化管理面板（单用户轻量版）

一个人的 sing-box 管理面板：**全面监控** + 入站管理 + 订阅生成。面板本身不安装 sing-box，只接管你服务器上已有的 sing-box。

## 定位

- **只做外部接管**：假设 sing-box 已由你自行安装（手动 / 官方 release / 第三方一键脚本均可）
- 面板读写你指定的 config.json（默认 `/etc/sing-box/config.json`），写前自动备份、写后跑 `sing-box check` 校验、经 `systemctl restart` 生效
- 首次接管时把现有入站（ss/vmess/vless/trojan/hysteria2）导入面板

## 功能

- **📊 监控大屏**（哪吒探针风格）：实时上/下行速率曲线（ECharts，每秒刷新，保留 60 秒）、当前值 + 峰值、总流量、按入站流量统计、CPU/内存/硬盘/负载、入站状态卡片（在线/端口/协议/连接数）
- **📈 历史流量**：每分钟持久化到 sqlite；近 24 小时曲线、近 30 天柱状图
- **🔌 实时连接**：当前活跃连接列表（客户端 IP、目标域名/IP、协议/入站、上下行、时长），支持手动断开
- **❤️ 服务健康**：定时检查 `systemctl is-active`，服务挂掉时红色横幅告警 + 一键重启
- **📝 操作审计**：登录、增删改入站、重启服务等操作记入 sqlite，前端可查（保留 2000 条）
- **🔌 入站管理**：shadowsocks / vmess / vless / trojan / hysteria2 的增删改、启用/停用；vless/trojan 一键 reality 配置；hysteria2 自动生成自签名证书
- **👤 极简用户模型**：每个入站自带一个主凭证（自动生成），最多再加 2 个备用凭证，可一键重新生成
- **🔗 订阅**：每个入站一个订阅链接 + 本地二维码（绝不调用第三方 QR API）；聚合订阅 `/sub`（纯文本）
- **⚙️ 进程控制**：经 systemctl 启动/停止/重启 sing-box、读 journalctl 日志、配置校验
- **🔐 登录**：admin 单密码，默认 `admin`，首次登录强制修改

## 前置要求

服务器上已有 sing-box（面板不负责安装）。没有的话先装：

- 官方 releases：https://github.com/SagerNet/sing-box/releases
- 或一键脚本：`bash <(curl -sL https://raw.githubusercontent.com/fscarmen/sing-box/main/sing-box.sh)`

## 部署（推荐：裸机）

```bash
# 1. 复制代码
git clone https://github.com/cesar699/tianlong-panel.git /opt/singbox-panel
cd /opt/singbox-panel
pip install -r backend/requirements.txt

# 2. 安装为系统服务（面板监听 127.0.0.1:8080）
cp singbox-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now singbox-panel
```

浏览器打开 `http://服务器IP:8080`（建议经 nginx 反代并加密码/白名单），默认密码 `admin`，首次登录强制修改。

然后去「设置」→「sing-box 接管」：
1. 确认二进制路径（空=自动探测）、配置文件路径、服务名
2. 点「探测安装」确认三项都 ✅
3. 点「接管现有配置」（会先备份 `config.json.panel-bak-时间戳`，再导入现有入站）

## 部署（Docker，功能受限）

```bash
docker compose up -d --build
```

> Docker 内无 systemd：**启动/停止/重启按钮不可用**，请在宿主机执行 `systemctl`。
> 如需面板读写宿主机配置，把宿主机的 `/etc/sing-box` 挂载进容器（见 docker-compose.yml 注释），并映射 sing-box 二进制所在目录。

## 流量统计实现

sing-box 官方 release 二进制**不包含** v2ray_api（需自行编译），因此本面板采用 clash_api 方案：

- 面板生成的配置固定启用 `experimental.clash_api`（`127.0.0.1:19090`）；若你原配置里 clash api 端口不同，接管后会被统一成这个（原文件已备份）
- 后台每 2 秒轮询 `/traffic`（总量速率）与 `/connections`
- 按 `metadata.type = "协议/入站tag"` 把每条连接的流量归因到入站，增量持久化到 sqlite
- 每分钟把各入站 + 总量差值写入 `traffic_history`（保留 35 天），供 24h/30d 图表
- 实时速率曲线保留最近 60 秒，峰值可一键清零

## 目录结构

```
singbox-panel/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── singbox-panel.service   # 裸机部署的 systemd 单元
├── backend/
│   ├── main.py          # FastAPI 入口 + 静态前端托管
│   ├── db.py            # sqlite 数据层（含审计/历史流量）
│   ├── singbox.py       # 二进制探测（不下载）
│   ├── config_mgr.py    # config.json 生成 + 备份/回滚 + sing-box check 校验
│   ├── takeover.py      # 首次接管：解析外部配置导入入站
│   ├── inbounds.py      # 入站 CRUD + 凭证管理
│   ├── subscribe.py     # 订阅链接 + 本地二维码
│   ├── process.py       # systemctl start/stop/restart + journalctl 日志
│   ├── stats.py         # 流量轮询 + 速率曲线 + 历史记录 + 连接管理
│   └── requirements.txt
├── frontend/
│   └── index.html       # 单文件：Vue3 + Tailwind + ECharts（CDN）
└── data/                # volume 持久化：panel.db（审计/历史流量/入站）
```

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/login | 登录 |
| POST | /api/change-password | 改密码 |
| GET | /api/dashboard | 探针大屏数据（含服务存活/安装状态） |
| GET | /api/traffic/history?range=24h\|30d | 历史流量 |
| GET | /api/connections | 实时连接列表 |
| DELETE | /api/connections/{id} | 断开连接 |
| GET | /api/audit | 操作审计 |
| GET/POST | /api/inbounds | 入站列表/新建 |
| PUT/DELETE | /api/inbounds/{id} | 修改/删除 |
| POST | /api/inbounds/{id}/regen-credential | 重生成主凭证 |
| POST/DELETE | /api/inbounds/{id}/backups | 备用凭证增删 |
| GET | /api/inbounds/{id}/link | 订阅链接+二维码 |
| GET | /sub | 聚合订阅（纯文本） |
| GET | /api/singbox/detect | 探测安装 |
| POST | /api/singbox/takeover | 接管现有配置 |
| POST | /api/singbox/{start,stop,restart} | 进程控制（systemctl） |
| GET | /api/singbox/logs | 日志（journalctl） |
| POST | /api/singbox/check | 配置校验 |

除 `/api/login` 与 `/sub` 外，所有 `/api/*` 需要 `Authorization: Bearer <token>`。
