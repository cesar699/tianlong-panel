#!/bin/bash
# 天龙面板一键部署脚本（基于哪吒 Nezha 二次开发）
# 用法: curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/deploy.sh | bash
set -e

NEZHA_VERSION="9881c47f0cc92256ba203919da5a25fab79a6bad"
INSTALL_DIR="/opt/tianlong-panel"
PORT="8009"

echo "=== 天龙面板部署开始 ==="

if command -v apt-get >/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq git curl build-essential unzip 2>&1 | tail -1
elif command -v yum >/dev/null; then
    sudo yum install -y git curl gcc unzip 2>&1 | tail -1
fi

if ! command -v go >/dev/null || ! go version 2>/dev/null | grep -q "go1.26"; then
    echo "安装 Go 1.26.8..."
    GO_TGZ="/tmp/go.tgz"
    GO_URLS=(
        "https://dl.google.com/go/go1.26.8.linux-amd64.tar.gz"
        "https://mirrors.aliyun.com/golang/go1.26.8.linux-amd64.tar.gz"
        "https://go.dev/dl/go1.26.8.linux-amd64.tar.gz"
    )
    GO_OK=0
    for GO_URL in "${GO_URLS[@]}"; do
        echo "尝试下载: $GO_URL"
        rm -f "$GO_TGZ"
        if curl -sL --retry 2 --max-time 300 -o "$GO_TGZ" "$GO_URL"; then
            GO_SIZE=$(stat -c%s "$GO_TGZ" 2>/dev/null || echo 0)
            if [ "$GO_SIZE" -gt 60000000 ]; then
                echo "下载成功 ($GO_SIZE 字节)"
                GO_OK=1
                break
            else
                echo "下载不完整 ($GO_SIZE 字节)，换源..."
            fi
        else
            echo "下载失败，换源..."
        fi
    done
    if [ "$GO_OK" -ne 1 ]; then
        echo "ERROR: Go 下载失败，请检查网络或手动安装 Go 1.26.8+"
        exit 1
    fi
    sudo rm -rf /usr/local/go
    sudo tar -C /usr/local -xzf "$GO_TGZ"
    rm -f "$GO_TGZ"
fi
export PATH=$PATH:/usr/local/go/bin
go version

WORKDIR=$(mktemp -d)
cd "$WORKDIR"
echo "拉取哪吒源码..."
git clone --depth 1 https://github.com/nezhahq/nezha.git nezha
cd nezha
git fetch --depth 1 origin "$NEZHA_VERSION" 2>/dev/null || true
git checkout "$NEZHA_VERSION" 2>/dev/null || true

echo "应用天龙补丁..."
curl -sL --max-time 60 -o /tmp/tianlong.patch https://raw.githubusercontent.com/cesar699/tianlong-panel/main/tianlong.patch
patch -p1 < /tmp/tianlong.patch || { echo "补丁应用失败"; exit 1; }

echo "复制天龙功能包..."
mkdir -p service/tianlong/web
for f in model.go tianlong.go attack.go disk.go ai.go web.go; do
    curl -sL --max-time 30 -o "service/tianlong/$f" "https://raw.githubusercontent.com/cesar699/tianlong-panel/main/service/tianlong/$f"
done
curl -sL --max-time 30 -o service/tianlong/web/map.html "https://raw.githubusercontent.com/cesar699/tianlong-panel/main/service/tianlong/web/map.html"
curl -sL --max-time 30 -o service/tianlong/web/world.svg "https://raw.githubusercontent.com/cesar699/tianlong-panel/main/service/tianlong/web/world.svg"

echo "下载前端..."
for t in "admin-dist|https://github.com/nezhahq/admin-frontend|v2.3.8" "user-dist|https://github.com/hamster1963/nezha-dash-v2|v2.4.3"; do
    path=$(echo $t | cut -d'|' -f1); repo=$(echo $t | cut -d'|' -f2); ver=$(echo $t | cut -d'|' -f3)
    d=$(mktemp -d) && cd $d && curl -sL --max-time 120 -o dist.zip "$repo/releases/download/$ver/dist.zip" && unzip -q dist.zip && rm -rf "$WORKDIR/nezha/cmd/dashboard/$path" && mv dist "$WORKDIR/nezha/cmd/dashboard/$path" && cd "$WORKDIR/nezha" && rm -rf $d
done

echo "编译中（约 3-5 分钟）..."
mkdir -p cmd/dashboard/docs
cat > cmd/dashboard/docs/docs.go <<'EOF'
package docs
var SwaggerInfo = struct{ Version string }{Version: "tianlong"}
EOF
export GOTOOLCHAIN=go1.26.8
export TMPDIR=/tmp
CGO_ENABLED=1 go build -tags go_json -trimpath -buildvcs=false -ldflags "-s -w" -o tianlong-panel ./cmd/dashboard

echo "安装到 $INSTALL_DIR..."
sudo mkdir -p "$INSTALL_DIR"
sudo cp tianlong-panel "$INSTALL_DIR/"
sudo chmod +x "$INSTALL_DIR/tianlong-panel"

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    sudo tee "$INSTALL_DIR/config.yaml" > /dev/null <<EOF
listen_port: $PORT
language: zh_CN
site_name: "天龙面板"
EOF
fi

sudo tee /etc/systemd/system/tianlong-panel.service > /dev/null <<EOF
[Unit]
Description=天龙面板 (基于哪吒)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/tianlong-panel -c $INSTALL_DIR/config.yaml -db $INSTALL_DIR/sqlite.db
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable tianlong-panel
sudo systemctl restart tianlong-panel
rm -rf "$WORKDIR" /tmp/tianlong.patch

echo ""
echo "=== 天龙面板部署完成 ==="
echo "面板地址: http://$(curl -s -m 5 ifconfig.me 2>/dev/null || echo '服务器IP'):$PORT"
echo "默认账号: admin / admin（首次登录）"
echo "独家功能:"
echo "  - 全球攻击地图: http://IP:$PORT/tianlong/map"
echo "  - AI 运维助手: POST http://IP:$PORT/tianlong/api/ask"
echo "  - 磁盘写满预测: http://IP:$PORT/tianlong/api/disk-prediction"
