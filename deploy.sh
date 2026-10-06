#!/bin/bash
# 天龙面板服务端一键部署脚本
# 用法: curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/deploy.sh | bash
# 可选环境变量: PANEL_PORT(默认8009) PANEL_SECRET(默认随机生成)
set -e

PANEL_PORT="${PANEL_PORT:-8009}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tianlong-panel}"
REPO_RAW="https://raw.githubusercontent.com/cesar699/tianlong-panel/main"

echo "🐉 天龙面板服务端一键部署"

# 1. 系统依赖：python3 / git / curl / venv
need=""
command -v python3 >/dev/null || need="$need python3"
command -v git >/dev/null || need="$need git"
command -v curl >/dev/null || need="$need curl"
python3 -c "import ensurepip" 2>/dev/null || need="$need python3-venv"
if [ -n "$need" ]; then
  echo "→ 安装系统依赖:$need"
  if command -v apt-get >/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq $need
  elif command -v yum >/dev/null; then
    sudo yum install -y -q python3 git curl
  else
    echo "请手动安装:$need"; exit 1
  fi
fi

# 2. 拉代码
echo "→ 拉取代码到 $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  sudo git -C "$INSTALL_DIR" pull -q 2>/dev/null || true
else
  sudo rm -rf "$INSTALL_DIR"
  git clone -q "https://github.com/cesar699/tianlong-panel.git" "$INSTALL_DIR" 2>/dev/null \
    || { echo "git clone 失败，请先安装 git"; exit 1; }
fi
cd "$INSTALL_DIR"
sudo chown -R "$(whoami)" "$INSTALL_DIR"

# 3. 虚拟环境 + 依赖（重跑时先清掉坏掉的 venv）
echo "→ 安装依赖"
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# 4. 生成配置
if [ -z "$PANEL_SECRET" ]; then
  PANEL_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))")
  echo "→ 已生成随机密钥（agent 也要用这个）: $PANEL_SECRET"
fi
cat > server/server.json <<EOF
{
  "secret": "$PANEL_SECRET",
  "port": $PANEL_PORT,
  "checks": []
}
EOF
echo "→ 配置已写入 server/server.json"

# 5. systemd 服务
echo "→ 注册系统服务"
PYBIN="$INSTALL_DIR/.venv/bin/python"
sudo tee /etc/systemd/system/tianlong-panel.service >/dev/null <<EOF
[Unit]
Description=天龙面板 Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$PYBIN $INSTALL_DIR/server/tianlong-server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q tianlong-panel
sudo systemctl restart tianlong-panel
sleep 3

# 6. 验证
if sudo systemctl is-active -q tianlong-panel; then
  IP=$(curl -s -m 5 ifconfig.me 2>/dev/null || echo "服务器IP")
  echo ""
  echo "✅ 部署成功！看板地址: http://$IP:$PANEL_PORT"
  echo "📌 agent 用的密钥: $PANEL_SECRET（装 agent 时填这个）"
else
  echo "❌ 服务启动失败，看日志: sudo journalctl -u tianlong-panel -n 50"
  exit 1
fi
