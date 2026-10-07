package tianlong

import (
	"log"
	"time"

	"github.com/nezhahq/nezha/model"
	"github.com/nezhahq/nezha/pkg/tsdb"
	"github.com/nezhahq/nezha/service/singleton"
)

// diskWatchLoop 每天检查一次磁盘写满预测
func diskWatchLoop() {
	time.Sleep(60 * time.Second)
	ticker := time.NewTicker(24 * time.Hour)
	defer ticker.Stop()
	for {
		checkDiskPredictions()
		<-ticker.C
	}
}

// PredictDisk 对单台服务器做线性回归，返回每天增长百分点和预计写满天数
func PredictDisk(serverID uint64) (usagePct, dailyGrow float64, etaDays *float64) {
	if !singleton.TSDBEnabled() || singleton.TSDBShared == nil {
		return 0, 0, nil
	}
	period, err := tsdb.ParseQueryPeriod("7d")
	if err != nil {
		return 0, 0, nil
	}
	points, err := singleton.TSDBShared.QueryServerMetrics(serverID, tsdb.MetricServerDisk, period)
	if err != nil || len(points) < 10 {
		return 0, 0, nil
	}
	// 线性回归：x=天数，y=使用率%
	n := float64(len(points))
	var sx, sy, sxx, sxy float64
	t0 := float64(points[0].Timestamp) / 86400000.0
	for _, p := range points {
		x := float64(p.Timestamp)/86400000.0 - t0
		y := p.Value
		sx += x
		sy += y
		sxx += x * x
		sxy += x * y
	}
	denom := n*sxx - sx*sx
	if denom == 0 {
		return points[len(points)-1].Value, 0, nil
	}
	slope := (n*sxy - sx*sy) / denom // 每天增长百分点
	usagePct = points[len(points)-1].Value
	if slope <= 0.01 {
		return usagePct, slope, nil // 不增长，无风险
	}
	days := (100 - usagePct) / slope
	if days < 0 {
		days = 0
	}
	return usagePct, slope, &days
}

func checkDiskPredictions() {
	for _, s := range singleton.ServerShared.GetList() {
		if s == nil {
			continue
		}
		usage, grow, eta := PredictDisk(s.ID)
		if eta != nil && *eta < 7 {
			msg := sprintfDiskWarn(s.Name, usage, grow, *eta)
			log.Printf("天龙>> %s", msg)
			srv := *s
			singleton.NotificationShared.SendNotification(0, msg, "", &srv)
		}
	}
}

func sprintfDiskWarn(name string, usage, grow, eta float64) string {
	return "[天龙] " + name + " 磁盘按当前增速约 " +
		formatFloat(eta, 1) + " 天后写满（当前 " + formatFloat(usage, 1) +
		"%，每天 +" + formatFloat(grow, 2) + "%）"
}

func formatFloat(f float64, prec int) string {
	// 简单格式化，避免引入 strconv 冗余
	mult := 1.0
	for i := 0; i < prec; i++ {
		mult *= 10
	}
	v := float64(int(f*mult+0.5)) / mult
	s := ""
	neg := false
	if v < 0 {
		neg = true
		v = -v
	}
	intPart := int(v)
	fracPart := int((v - float64(intPart)) * mult)
	s = itoa(intPart) + "."
	fs := itoa(fracPart)
	for len(fs) < prec {
		fs = "0" + fs
	}
	s += fs
	if neg {
		s = "-" + s
	}
	return s
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}

// GetAllDiskPredictions 供 API 调用
func GetAllDiskPredictions() []DiskPrediction {
	var out []DiskPrediction
	for _, s := range singleton.ServerShared.GetList() {
		if s == nil {
			continue
		}
		usage, grow, eta := PredictDisk(s.ID)
		out = append(out, DiskPrediction{
			ServerID:   s.ID,
			ServerName: s.Name,
			UsagePct:   usage,
			DailyGrow:  grow,
			EtaDays:    eta,
		})
	}
	return out
}

var _ = model.TaskTypeCommand // 避免未使用导入（如有）
