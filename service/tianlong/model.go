package tianlong

// 天龙面板独家功能包（基于哪吒 Nezha 二次开发，Apache-2.0）
//  - 磁盘写满预测（线性回归）
//  - SSH 爆破监控 + 全球攻击地图
//  - AI 运维助手

import (
	"time"
)

// Probe 一次 SSH 探测上报记录
type Probe struct {
	ID       uint64    `gorm:"primaryKey"`
	ServerID uint64    `gorm:"index:idx_probe_server_ts"`
	Server   string    // 服务器名（冗余，方便展示）
	IP       string    `gorm:"index:idx_probe_ip_ts"`
	Attempts int
	TS       time.Time `gorm:"index:idx_probe_server_ts;index:idx_probe_ip_ts"`
}

// GeoIP IP 归属地缓存
type GeoIP struct {
	IP      string `gorm:"primaryKey"`
	Country string
	City    string
	Lat     float64
	Lon     float64
	Updated time.Time
}

// AttackIP 聚合后的攻击 IP（API 返回用）
type AttackIP struct {
	IP       string   `json:"ip"`
	Attempts int      `json:"attempts"`
	Servers  []string `json:"servers"`
	LastSeen int64    `json:"last_seen"`
	Country  string   `json:"country"`
	City     string   `json:"city"`
	Lat      *float64 `json:"lat"`
	Lon      *float64 `json:"lon"`
}

// DiskPrediction 磁盘预测结果
type DiskPrediction struct {
	ServerID   uint64  `json:"server_id"`
	ServerName string  `json:"server_name"`
	UsagePct   float64 `json:"usage_pct"`
	DailyGrow  float64 `json:"daily_grow_pct"` // 每天增长百分点
	EtaDays    *float64 `json:"eta_days"`      // 预计写满天数（nil=无风险）
}
