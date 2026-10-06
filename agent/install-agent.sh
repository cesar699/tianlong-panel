#!/bin/bash
# 天龙面板 Agent 一键安装脚本（在被监控的服务器上执行）
# 用法: curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/agent/install-agent.sh | \
#         sudo PANEL_SERVER="http://面板IP:8009" PANEL_SECRET="密钥" PANEL_NAME="web-01" bash
set -e

: "${PANEL_SERVER:?请设置 PANEL_SERVER，如 http://1.2.3.4:8009}"
: "${PANEL_SECRET:?请设置 PANEL_SECRET（和服务端一致）}"
PANEL_NAME="${PANEL_NAME:-$(hostname)}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tianlong-panel}"

echo "🐉 天龙面板 Agent 一键安装 [$PANEL_NAME]"

if ! command -v python3 >/dev/null; then
  echo "请先安装 python3"; exit 1
fi

echo "→ 下载 agent"
sudo mkdir -p "$INSTALL_DIR/agent"
curl -sL "https://raw.githubusercontent.com/cesar699/tianlong-panel/main/agent/tianlong-agent.py" \
  -o /tmp/tianlong-agent.py
sudo mv /tmp/tianlong-agent.py "$INSTALL_DIR/agent/"
if ! sudo python3 -c "import pip" 2>/dev/null; then
  if command -v apt-get >/dev/null; then sudo apt-get install -y -qq python3-pip; fi
fi
sudo python3 -m pip install -q psutil requests 2>/dev/null \
  || sudo python3 -m pip install -q --break-system-packages psutil requests

sudo tee "$INSTALL_DIR/agent/agent.json" >/dev/null <<EOF
{
  "server": "$PANEL_SERVER",
  "secret": "$PANEL_SECRET",
  "name": "$PANEL_NAME",
  "interval": 10
}
EOF
echo "→ 配置已写入"

echo "→ 注册系统服务"
sudo tee /etc/systemd/system/tianlong-agent.service >/dev/null <<EOF
[Unit]
Description=天龙面板 Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/agent
ExecStart=/usr/bin/python3 $INSTALL_DIR/agent/tianlong-agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q tianlong-agent
sudo systemctl restart tianlong-agent
sleep 2

if sudo systemctl is-active -q tianlong-agent; then
  echo "✅ Agent 安装成功，正在上报到 $PANEL_SERVER"
else
  echo "❌ 启动失败: sudo journalctl -u tianlong-agent -n 50"
  exit 1
fi
