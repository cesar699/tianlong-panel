# 🐉 天龙面板

服务器监控：实时看板、智能告警、应用层监控、AI 运维助手。

## 功能

| 功能 | 说明 |
|---|---|
| 实时看板 | 所有服务器 CPU / 内存 / 磁盘 / 负载 / 流量，一目了然，24h 曲线 |
| 阈值告警 | CPU / 内存 / 磁盘超限、服务器离线自动告警 |
| **磁盘写满预测** | 线性回归预测磁盘还有几天写满，提前预警（哪吒没有） |
| 应用层监控 | HTTP / TCP 端口存活、SSL 证书过期天数 |
| **AI 运维助手** | 直接问「哪台机器最卡？」「磁盘还能撑多久？」——无 Key 时用内置规则引擎，有 Key 时接 LLM 深度分析 |
| 进程透视 | 每台机器最占资源的 Top 进程 |
| 🛡️ **SSH 爆破监控** | agent 解析登录日志，统计扫描 22 端口的 IP（v2 新增） |
| 🌍 **全球攻击地图** | 攻击 IP 按归属地打在世界地图上，`/map` 页面（v2 新增，哪吒没有） |
| 🚨 **爆破告警** | 单个 IP 10 分钟尝试超 100 次自动 critical 告警（v2 新增） |
| 📋 **复制即装** | 看板"添加服务器"区自动生成带地址密钥的 agent 一键安装命令，复制粘贴即用（v2 新增） |

## 目录结构

```
tianlong-panel/
├── agent/
│   ├── tianlong-agent.py    # 采集端：装到每台被监控的服务器上
│   └── agent.json.example   # agent 配置示例
├── server/
│   ├── tianlong-server.py   # 服务端：API + 告警引擎 + 看板
│   ├── dashboard.html       # 看板页面（单文件）
│   └── server.json.example  # 服务端配置示例
└── requirements.txt
```

## 快速开始

### 1. 服务端（面板）

```bash
cd tianlong-panel
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 配置：复制示例并改密钥（agent 也要用同一个）
cp server/server.json.example server/server.json
# 编辑 server.json：secret 改成你自己的随机字符串，port 默认 8009
# 可选：在 checks 里加应用监控；加 llm_key 则启用 AI 深度分析

.venv/bin/python server/tianlong-server.py
# 打开 http://服务器IP:8009
```

### 2. 采集端（每台被监控的服务器）

```bash
# 把 agent/ 目录拷到目标服务器
pip install psutil requests
cp agent/agent.json.example agent/agent.json
# 编辑 agent.json：server 填面板地址，secret 填同一个密钥，name 取个名字

python3 agent/tianlong-agent.py
# 建议用 systemd / supervisor 做成服务，开机自启
```

systemd 示例（`/etc/systemd/system/tianlong-agent.service`）：

```ini
[Unit]
Description=天龙面板 Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/tianlong-panel/agent/tianlong-agent.py
WorkingDirectory=/opt/tianlong-panel/agent
Restart=always

[Install]
WantedBy=multi-user.target
```

### 3. AI 运维助手（可选）

在 `server.json` 里加：

```json
{
  "llm_key": "你的 Model API key",
  "llm_base": "https://api.meta.ai/v1",
  "llm_model": "muse-spark-1.3"
}
```

不配 key 也能用：内置规则引擎能回答「哪台最卡」「磁盘多久满」「有什么告警」「最占资源的进程」这类问题。

## API

| 接口 | 说明 |
|---|---|
| `POST /api/report` | agent 上报（需 secret） |
| `GET /api/servers` | 服务器列表 + 最新指标 |
| `GET /api/metrics/{name}?hours=24` | 历史曲线 |
| `GET /api/alerts` | 告警列表 |
| `GET/POST /api/checks` | 应用监控 |
| `POST /api/ask` | AI 运维问答 `{"question":"..."}` |

## 和哪吒面板的区别

- 磁盘写满**预测**（不只是阈值）
- SSL 证书过期监控
- AI 问答式运维（自然语言查状态、诊断）
- 单文件看板，无构建步骤，部署只要 Python

## 安全提醒

- `secret` 是 agent 接入的唯一凭证，改成足够随机的字符串，不要用默认的
- 面板建议放内网或加反向代理 + 密码保护后再暴露公网
- 数据存在 `server/tianlong.db`（SQLite），保留 7 天自动清理
