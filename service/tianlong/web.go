package tianlong

import (
	"embed"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
)

//go:embed web/map.html web/world.svg
var webFS embed.FS

// RegisterRoutes 注册天龙独家功能路由（在 controller.routers 中调用）
func RegisterRoutes(r *gin.Engine) {
	tl := r.Group("/tianlong")

	// 页面
	tl.GET("/map", func(c *gin.Context) {
		data, _ := webFS.ReadFile("web/map.html")
		c.Data(http.StatusOK, "text/html; charset=utf-8", data)
	})
	tl.GET("/world.svg", func(c *gin.Context) {
		data, _ := webFS.ReadFile("web/world.svg")
		c.Data(http.StatusOK, "image/svg+xml", data)
	})

	// API
	tl.GET("/api/attacks", func(c *gin.Context) {
		hours, _ := strconv.Atoi(c.DefaultQuery("hours", "24"))
		c.JSON(http.StatusOK, gin.H{
			"attacks": GetAttacks(hours),
			"ts":      time.Now().Unix(),
		})
	})
	tl.GET("/api/disk-prediction", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{
			"predictions": GetAllDiskPredictions(),
		})
	})
	tl.POST("/api/ask", func(c *gin.Context) {
		var req struct {
			Question string `json:"question"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "bad request"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"answer": Ask(req.Question)})
	})
	// 调试：手动触发探测采集
	tl.POST("/api/probe-now", func(c *gin.Context) {
		go collectProbes()
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
}
