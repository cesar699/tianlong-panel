package tianlong

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/nezhahq/nezha/service/singleton"
)

// Ask 处理 AI 运维问答：先规则引擎，再可选 LLM
func Ask(question string) string {
	q := strings.ToLower(question)

	// 攻击相关
	if strings.Contains(q, "22") || strings.Contains(q, "扫描") || strings.Contains(q, "爆破") || strings.Contains(q, "攻击") {
		attacks := GetAttacks(24)
		if len(attacks) == 0 {
			return "过去 24 小时没有检测到扫描 22 端口的行为 🎉"
		}
		var b strings.Builder
		b.WriteString("过去 24h 扫描 22 端口最多的 IP：\n")
		for i, a := range attacks {
			if i >= 5 {
				break
			}
			geo := ""
			if a.Country != "" {
				geo = "（" + a.Country + " " + a.City + "）"
			}
			fmt.Fprintf(&b, "%d. %s%s — %d 次，目标：%s\n", i+1, a.IP, geo, a.Attempts, strings.Join(a.Servers, ","))
		}
		return b.String()
	}

	// 磁盘预测相关
	if strings.Contains(q, "磁盘") || strings.Contains(q, "写满") || strings.Contains(q, "硬盘") {
		preds := GetAllDiskPredictions()
		var b strings.Builder
		b.WriteString("磁盘写满预测：\n")
		found := false
		for _, p := range preds {
			if p.EtaDays != nil && *p.EtaDays < 30 {
				fmt.Fprintf(&b, "- %s：当前 %.1f%%，预计 %.1f 天后写满 ⚠️\n", p.ServerName, p.UsagePct, *p.EtaDays)
				found = true
			}
		}
		if !found {
			b.WriteString("所有服务器磁盘近期无写满风险 ✅")
		}
		return b.String()
	}

	// 最卡 / 负载相关
	if strings.Contains(q, "卡") || strings.Contains(q, "负载") || strings.Contains(q, "慢") {
		var worst string
		var worstLoad float64 = -1
		for _, s := range singleton.ServerShared.GetList() {
			if s == nil || s.State == nil {
				continue
			}
			if s.State.Load1 > worstLoad {
				worstLoad = s.State.Load1
				worst = s.Name
			}
		}
		if worst == "" {
			return "暂无服务器数据"
		}
		return fmt.Sprintf("当前负载最高的服务器是 %s（load %.2f）", worst, worstLoad)
	}

	// 服务器状态总览
	if strings.Contains(q, "状态") || strings.Contains(q, "在线") || strings.Contains(q, "总览") {
		online, total := 0, 0
		for _, s := range singleton.ServerShared.GetList() {
			if s == nil {
				continue
			}
			total++
			if time.Since(s.LastActive) < 2*time.Minute {
				online++
			}
		}
		return fmt.Sprintf("共 %d 台服务器，%d 台在线", total, online)
	}

	// 尝试 LLM
	if ans := askLLM(question); ans != "" {
		return ans
	}
	return "我能回答：服务器状态、哪台最卡、磁盘写满预测、谁在扫 22 端口。换个问法试试？"
}

// askLLM 可选的 LLM 透传（环境变量配置）
func askLLM(question string) string {
	apiKey := os.Getenv("TIANLONG_LLM_KEY")
	if apiKey == "" {
		return ""
	}
	base := os.Getenv("TIANLONG_LLM_BASE")
	if base == "" {
		base = "https://api.meta.ai/v1"
	}
	model := os.Getenv("TIANLONG_LLM_MODEL")
	if model == "" {
		model = "muse-spark-1.3"
	}
	ctx := buildContext()
	body, _ := json.Marshal(map[string]interface{}{
		"model": model,
		"messages": []map[string]string{
			{"role": "system", "content": "你是天龙面板的 AI 运维助手，用简洁中文回答。\n" + ctx},
			{"role": "user", "content": question},
		},
		"max_tokens": 500,
	})
	req, _ := http.NewRequest("POST", base+"/chat/completions", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var result struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if json.Unmarshal(data, &result) != nil || len(result.Choices) == 0 {
		return ""
	}
	return result.Choices[0].Message.Content
}

func buildContext() string {
	var b strings.Builder
	b.WriteString("服务器状态：\n")
	for _, s := range singleton.ServerShared.GetList() {
		if s == nil || s.State == nil {
			continue
		}
		online := "离线"
		if time.Since(s.LastActive) < 2*time.Minute {
			online = "在线"
		}
		var memTotal, diskTotal uint64 = 1, 1
		if s.Host != nil {
			if s.Host.MemTotal > 0 {
				memTotal = s.Host.MemTotal
			}
			if s.Host.DiskTotal > 0 {
				diskTotal = s.Host.DiskTotal
			}
		}
		fmt.Fprintf(&b, "- %s(%s): CPU %.1f%% 内存 %.1f%% 磁盘 %.1f%%\n",
			s.Name, online, s.State.CPU, float64(s.State.MemUsed)*100.0/float64(memTotal),
			float64(s.State.DiskUsed)*100.0/float64(diskTotal))
	}
	attacks := GetAttacks(24)
	if len(attacks) > 0 {
		b.WriteString("24h 内扫描 22 端口最多的 IP：\n")
		for i, a := range attacks {
			if i >= 5 {
				break
			}
			fmt.Fprintf(&b, "- %s: %d 次\n", a.IP, a.Attempts)
		}
	}
	return b.String()
}
