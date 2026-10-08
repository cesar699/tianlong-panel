#!/usr/bin/env bash
# sing-box 管理面板一键更新
# 用法: curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/update.sh | bash
set -e

PANEL_DIR="/opt/singbox-panel"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行"; exit 1
fi
if [ ! -d "$PANEL_DIR/.git" ]; then
  echo "没找到面板安装目录 $PANEL_DIR，请先用 install.sh 安装"; exit 1
fi

echo "=== 备份数据 ==="
cp -r "$PANEL_DIR/data" /tmp/singbox-panel-data-bak 2>/dev/null || true

echo "=== 拉取最新代码 ==="
cd "$PANEL_DIR"
git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "已经是最新版，无需更新"
  rm -rf /tmp/singbox-panel-data-bak
  exit 0
fi
git reset --hard origin/main

echo "=== 更新依赖 ==="
pip install -q -r backend/requirements.txt 2>&1 | tail -1 || pip install -q --break-system-packages -r backend/requirements.txt 2>&1 | tail -1

echo "=== 恢复数据并重启 ==="
[ -d /tmp/singbox-panel-data-bak ] && rm -rf "$PANEL_DIR/data" && cp -r /tmp/singbox-panel-data-bak "$PANEL_DIR/data" && rm -rf /tmp/singbox-panel-data-bak || true
systemctl restart singbox-panel
sleep 3
systemctl is-active singbox-panel && echo "更新完成，面板运行中" || { echo "面板启动失败: journalctl -u singbox-panel"; exit 1; }
