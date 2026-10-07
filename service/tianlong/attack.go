package tianlong

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/nezhahq/nezha/model"
	"github.com/nezhahq/nezha/proto"
	"github.com/nezhahq/nezha/service/singleton"
)

// 探测脚本：在 agent 上解析 SSH 登录日志，输出 JSON
// 优先 python3（带 10 分钟时间窗），否则降级为 awk 粗略统计
const probeScript = `
LOG=""; for f in /var/log/auth.log /var/log/secure; do [ -r "$f" ] && LOG="$f" && break; done
if [ -z "$LOG" ]; then echo "[]"; exit 0; fi
if command -v python3 >/dev/null 2>&1; then
python3 - "$LOG" <<'PYEOF'
import sys, re, time, json
from datetime import datetime
cutoff = time.time() - 600
counts = {}
yr = datetime.now().year
pats = [re.compile(r"Failed password.*? from (\d+\.\d+\.\d+\.\d+)"),
        re.compile(r"Invalid user .*? from (\d+\.\d+\.\d+\.\d+)")]
try:
    with open(sys.argv[1], errors="ignore") as f:
        lines = f.readlines()[-5000:]
except Exception:
    print("[]"); sys.exit(0)
for line in lines:
    ip = None
    for p in pats:
        m = p.search(line)
        if m:
            ip = m.group(1)
            break
    if not ip:
        continue
    try:
        ts = datetime.strptime("%d %s" % (yr, line[:15]), "%Y %b %d %H:%M:%S").timestamp()
    except Exception:
        continue
    if ts >= cutoff:
        counts[ip] = counts.get(ip, 0) + 1
print(json.dumps([{"ip": k, "attempts": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])[:50]]))
PYEOF
else
grep -h "Failed password" "$LOG" 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | sort | uniq -c | sort -rn | head -20 | awk 'BEGIN{printf "["} {printf "%s{\"ip\":\"%s\",\"attempts\":%d}", (NR>1?",":""), $2, $1} END{print "]"}'
fi
`

// probeLoop 每 10 分钟向所有在线 agent 发送探测任务
func probeLoop() {
	time.Sleep(30 * time.Second) // 等 agent 上线
	ticker := time.NewTicker(10 * time.Minute)
	defer ticker.Stop()
	for {
		collectProbes()
		<-ticker.C
	}
}

func collectProbes() {
	log.Printf("天龙>> 开始采集 SSH 探测")
	count := 0
	for _, s := range singleton.ServerShared.GetList() {
		if s == nil {
			continue
		}
		count++
		taskID := nextTaskID()
		task := &proto.Task{
			Id:   taskID,
			Type: uint64(model.TaskTypeCommand),
			Data: probeScript,
		}
		if err := s.SendTask(task); err != nil {
			log.Printf("天龙>> 发送探测任务到 %s 失败: %v", s.Name, err)
			continue // agent 离线，跳过
		}
		log.Printf("天龙>> 已发送探测任务到 %s (taskID=%d)", s.Name, taskID)
		registerProbeTask(taskID, s.ID)
	}
	log.Printf("天龙>> 探测任务发送完成，共 %d 台服务器", count)
}

// HandleProbeResult 处理探测任务的结果（从 RequestTask handler 调用）
func HandleProbeResult(taskID uint64, data string, successful bool) bool {
	serverID, ok := lookupProbeTask(taskID)
	if !ok {
		return false
	}
	log.Printf("天龙>> 收到探测结果 taskID=%d serverID=%d 成功=%v 数据长度=%d", taskID, serverID, successful, len(data))
	if !successful {
		return true
	}
	var probes []struct {
		IP       string `json:"ip"`
		Attempts int    `json:"attempts"`
	}
	data = strings.TrimSpace(data)
	if err := json.Unmarshal([]byte(data), &probes); err != nil {
		log.Printf("天龙>> 探测结果解析失败: %v", err)
		return true
	}
	var serverName string
	if s, ok := singleton.ServerShared.Get(serverID); ok && s != nil {
		serverName = s.Name
	}
	now := time.Now()
	for _, p := range probes {
		if p.IP == "" || p.Attempts <= 0 {
			continue
		}
		singleton.DB.Create(&Probe{
			ServerID: serverID,
			Server:   serverName,
			IP:       p.IP,
			Attempts: p.Attempts,
			TS:       now,
		})
	}
	// 爆破告警：单 IP 10 分钟超 100 次
	for _, p := range probes {
		if p.Attempts > 100 {
			msg := fmt.Sprintf("[天龙] %s 疑似遭 SSH 爆破：%s 10分钟内尝试 %d 次", serverName, p.IP, p.Attempts)
			log.Printf("天龙>> %s", msg)
			// 走哪吒的通知渠道
			var srv model.Server
			if s, ok := singleton.ServerShared.Get(serverID); ok && s != nil {
				srv = *s
				singleton.NotificationShared.SendNotification(0, msg, "", &srv)
			}
		}
	}
	return true
}

// geoLoop 每 2 分钟为未知 IP 查询归属地
func geoLoop() {
	ticker := time.NewTicker(2 * time.Minute)
	defer ticker.Stop()
	for {
		lookupGeo()
		<-ticker.C
	}
}

func lookupGeo() {
	var ips []string
	singleton.DB.Model(&Probe{}).
		Where("ts > ?", time.Now().Add(-24*time.Hour)).
		Where("ip NOT IN (?)", singleton.DB.Model(&GeoIP{}).Select("ip")).
		Distinct().Pluck("ip", &ips)
	// 过滤内网 IP，限制批量
	var targets []string
	for _, ip := range ips {
		if strings.HasPrefix(ip, "10.") || strings.HasPrefix(ip, "192.168.") ||
			strings.HasPrefix(ip, "172.16.") || ip == "127.0.0.1" {
			continue
		}
		targets = append(targets, ip)
		if len(targets) >= 90 {
			break
		}
	}
	if len(targets) == 0 {
		return
	}
	results := batchGeoLookup(targets)
	for _, r := range results {
		if r.Status == "success" {
			singleton.DB.Save(&GeoIP{
				IP:      r.Query,
				Country: r.Country,
				City:    r.City,
				Lat:     r.Lat,
				Lon:     r.Lon,
				Updated: time.Now(),
			})
		}
	}
}

type geoResult struct {
	Status  string  `json:"status"`
	Query   string  `json:"query"`
	Country string  `json:"country"`
	City    string  `json:"city"`
	Lat     float64 `json:"lat"`
	Lon     float64 `json:"lon"`
}

func batchGeoLookup(ips []string) []geoResult {
	queries := make([]map[string]string, 0, len(ips))
	for _, ip := range ips {
		queries = append(queries, map[string]string{"query": ip})
	}
	body, _ := json.Marshal(queries)
	req, _ := http.NewRequest("POST",
		"http://ip-api.com/batch?fields=status,message,country,city,lat,lon,query",
		bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("天龙>> 归属地查询失败: %v", err)
		return nil
	}
	defer resp.Body.Close()
	var results []geoResult
	json.NewDecoder(resp.Body).Decode(&results)
	return results
}

// GetAttacks 聚合攻击数据（24h）
func GetAttacks(hours int) []AttackIP {
	if hours <= 0 {
		hours = 24
	}
	type row struct {
		IP       string
		Attempts int
		Servers  string
		LastSeen time.Time
	}
	var rows []row
	singleton.DB.Model(&Probe{}).
		Select("ip, SUM(attempts) as attempts, GROUP_CONCAT(DISTINCT server) as servers, MAX(ts) as last_seen").
		Where("ts > ?", time.Now().Add(-time.Duration(hours)*time.Hour)).
		Group("ip").Order("attempts DESC").Limit(200).Scan(&rows)

	var out []AttackIP
	for _, r := range rows {
		a := AttackIP{
			IP:       r.IP,
			Attempts: r.Attempts,
			Servers:  strings.Split(r.Servers, ","),
			LastSeen: r.LastSeen.Unix(),
		}
		var g GeoIP
		if err := singleton.DB.First(&g, "ip = ?", r.IP).Error; err == nil {
			a.Country = g.Country
			a.City = g.City
			lat, lon := g.Lat, g.Lon
			a.Lat, a.Lon = &lat, &lon
		}
		out = append(out, a)
	}
	return out
}
