#!/usr/bin/env bash
# sing-box + 管理面板 一键安装脚本
# 只从 SagerNet 官方 releases 下载 sing-box，无第三方脚本
# 用法: curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/install.sh | bash
set -e

SB_VERSION="${SB_VERSION:-1.14.2}"
PANEL_DIR="/opt/singbox-panel"
PANEL_PORT="8080"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行"; exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  SB_ARCH="amd64" ;;
  aarch64)  SB_ARCH="arm64" ;;
  *) echo "不支持的架构: $ARCH"; exit 1 ;;
esac

echo "=== 1/5 安装 sing-box v${SB_VERSION}（官方 releases）==="
if ! command -v sing-box >/dev/null 2>&1; then
  TMPD=$(mktemp -d)
  URL="https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/sing-box-${SB_VERSION}-linux-${SB_ARCH}.tar.gz"
  echo "下载: $URL"
  curl -sL --retry 3 -o "$TMPD/sb.tgz" "$URL"
  tar -xzf "$TMPD/sb.tgz" -C "$TMPD"
  install -m 755 "$TMPD/sing-box-${SB_VERSION}-linux-${SB_ARCH}/sing-box" /usr/local/bin/sing-box
  rm -rf "$TMPD"
  echo "sing-box 装到 /usr/local/bin/sing-box"
else
  echo "sing-box 已存在: $(sing-box version | head -1)"
fi

echo "=== 2/5 准备 sing-box 配置 ==="
mkdir -p /etc/sing-box
if [ ! -f /etc/sing-box/config.json ]; then
  cat > /etc/sing-box/config.json <<'EOF'
{
  "log": {"level": "info"},
  "inbounds": [],
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:19090",
      "external_ui": "",
      "secret": ""
    }
  }
}
EOF
  echo "已生成初始 /etc/sing-box/config.json（含面板要用的 clash api）"
else
  echo "/etc/sing-box/config.json 已存在，跳过"
fi

echo "=== 3/5 注册 sing-box 系统服务 ==="
if [ ! -f /etc/systemd/system/sing-box.service ]; then
  cat > /etc/systemd/system/sing-box.service <<'EOF'
[Unit]
Description=sing-box proxy
After=network.target

[Service]
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi
systemctl enable --now sing-box
sleep 2
systemctl is-active sing-box && echo "sing-box 运行中" || { echo "sing-box 启动失败，看日志: journalctl -u sing-box"; exit 1; }

echo "=== 4/5 安装管理面板 ==="
if [ -d "$PANEL_DIR" ]; then
  echo "面板目录已存在，先备份旧数据"
  cp -r "$PANEL_DIR/data" /tmp/singbox-panel-data-bak 2>/dev/null || true
  rm -rf "$PANEL_DIR"
fi
git clone --depth 1 https://github.com/cesar699/tianlong-panel.git "$PANEL_DIR"
cd "$PANEL_DIR"
pip install -q -r backend/requirements.txt 2>&1 | tail -1 || pip install -q --break-system-packages -r backend/requirements.txt 2>&1 | tail -1
[ -d /tmp/singbox-panel-data-bak ] && cp -r /tmp/singbox-panel-data-bak "$PANEL_DIR/data" && rm -rf /tmp/singbox-panel-data-bak || true

echo "=== 5/5 注册面板系统服务 ==="
cp singbox-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now singbox-panel
sleep 3

echo ""
echo "=== 安装完成 ==="
echo "sing-box: $(sing-box version | head -1) → /usr/local/bin/sing-box"
echo "面板: http://服务器IP:${PANEL_PORT}（默认密码 admin，首次登录强制修改）"
echo "进去后「设置」→「sing-box 接管」→ 点「探测安装」三项 ✅ → 点「接管现有配置」"
