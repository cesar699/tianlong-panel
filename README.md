# 天龙面板 🐉

基于[哪吒监控 Nezha Monitoring](https://github.com/nezhahq/nezha)（Apache-2.0）二次开发的服务器监控面板。

## 与原版哪吒的区别

| 功能 | 原版哪吒 | 天龙面板 |
|------|---------|---------|
| 服务器监控 | ✅ | ✅ |
| 告警推送 | ✅ | ✅ |
| WebTerminal | ✅ | ✅ |
| 磁盘写满预测 | ❌ | ✅ 线性回归预测 |
| SSH 爆破监控 | ❌ | ✅ 自动采集 + 告警 |
| 全球攻击地图 | ❌ | ✅ `/tianlong/map` |
| AI 运维助手 | ❌ | ✅ `/tianlong/api/ask` |

## 一键部署

```bash
curl -sL https://raw.githubusercontent.com/cesar699/tianlong-panel/main/deploy.sh | bash
```

部署完成后访问 `http://服务器IP:8009`，默认账号 `admin` / `admin`。

**注意**: 编译需要 Go 1.26.8+（gRPC 流兼容性要求），部署脚本会自动安装。

## 独家功能

### 全球攻击地图
访问 `/tianlong/map` 查看 24 小时内扫描你服务器 22 端口的攻击来源，含国家/城市定位。

### AI 运维助手
```bash
curl -X POST http://IP:8009/tianlong/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"谁在扫我的22端口"}'
```

### 磁盘写满预测
基于 7 天磁盘使用历史做线性回归，预测写满时间，7 天内写满自动告警。

### SSH 爆破监控
每 10 分钟自动采集各服务器的 SSH 登录失败日志，单 IP 10 分钟超 100 次自动告警。

## 技术说明

- `tianlong.patch` — 对哪吒源码的修改补丁（锁定版本 `9881c47`）
- `service/tianlong/` — 天龙独家功能的 Go 源码包
- `deploy.sh` — 一键部署脚本

构建原理：拉取哪吒源码 → 应用补丁 → 复制天龙包 → 编译。

## 协议

Apache-2.0（继承自上游哪吒项目）。
