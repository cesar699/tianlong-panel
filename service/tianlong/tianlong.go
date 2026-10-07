package tianlong

import (
	"log"
	"sync"
	"time"

	"github.com/nezhahq/nezha/service/singleton"
)

// probeTaskIDs 正在等待结果的探测任务 ID -> 服务器 ID
var (
	probeMu      sync.Mutex
	probeTaskIDs = make(map[uint64]uint64)
	taskIDSeq   uint64 = 1 << 60 // 探测任务 ID 从高位开始，避免与 cron ID 冲突
)

// Init 初始化天龙功能：建表 + 启动后台任务
func Init() {
	if err := singleton.DB.AutoMigrate(&Probe{}, &GeoIP{}); err != nil {
		log.Printf("天龙>> 建表失败: %v", err)
		return
	}
	// 清理 7 天前的探测记录
	singleton.DB.Where("ts < ?", time.Now().Add(-7*24*time.Hour)).Delete(&Probe{})

	go probeLoop()
	go geoLoop()
	go diskWatchLoop()
	log.Printf("天龙>> 独家功能已启动：磁盘预测 / SSH爆破监控 / AI助手")
}

// nextTaskID 分配探测任务 ID
func nextTaskID() uint64 {
	probeMu.Lock()
	defer probeMu.Unlock()
	taskIDSeq++
	return taskIDSeq
}

// registerProbeTask 登记任务 ID
func registerProbeTask(taskID, serverID uint64) {
	probeMu.Lock()
	probeTaskIDs[taskID] = serverID
	probeMu.Unlock()
}

// lookupProbeTask 查询并删除（只处理一次）
func lookupProbeTask(taskID uint64) (uint64, bool) {
	probeMu.Lock()
	defer probeMu.Unlock()
	sid, ok := probeTaskIDs[taskID]
	if ok {
		delete(probeTaskIDs, taskID)
	}
	return sid, ok
}
