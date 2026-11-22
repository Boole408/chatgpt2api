package handler

import (
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/basketikun/chatgpt2api/internal/config"
	"github.com/basketikun/chatgpt2api/internal/service"
	"github.com/gin-gonic/gin"
)

func Identity(auth *service.AuthService) gin.HandlerFunc {
	return func(c *gin.Context) {
		authorization := c.GetHeader("Authorization")
		if authorization == "" && c.GetHeader("x-api-key") != "" {
			authorization = "Bearer " + c.GetHeader("x-api-key")
		}
		identity, err := auth.Authenticate(c.Request.Context(), authorization, true)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"detail": gin.H{"error": service.ErrUnauthorized.Error()}})
			return
		}
		c.Set("identity", identity)
		c.Next()
	}
}

func Admin(c *gin.Context) {
	identity, exists := c.Get("identity")
	if !exists || identity.(service.Identity).Role != "admin" {
		c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"detail": gin.H{"error": "需要管理员权限才能执行这个操作"}})
		return
	}
	c.Next()
}

func LimitKeyRequests(auth *service.AuthService) gin.HandlerFunc {
	return func(c *gin.Context) {
		// Only calls that can start work use the request budget.
		if c.Request.Method == http.MethodPost {
			if retry := auth.RetryAfter(identity(c), time.Now()); retry > 0 {
				c.Header("Retry-After", strconv.Itoa(retry))
				writeError(c, http.StatusTooManyRequests, fmt.Errorf("当前密钥请求过于频繁，请在 %d 秒后重试", retry))
				c.Abort()
				return
			}
		}
		c.Next()
	}
}

func allowModel(c *gin.Context, model string) bool {
	if identity(c).AllowsModel(model) {
		return true
	}
	writeError(c, http.StatusForbidden, fmt.Errorf("当前密钥无权访问模型 %s", model))
	return false
}

func LimitBody(cfg *config.Config) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, cfg.MaxUploadMB<<20)
		c.Next()
	}
}

func CORS(c *gin.Context) {
	c.Header("Access-Control-Allow-Origin", "*")
	c.Header("Access-Control-Allow-Headers", "*")
	c.Header("Access-Control-Allow-Methods", "*")
	if c.Request.Method == http.MethodOptions {
		c.AbortWithStatus(http.StatusNoContent)
		return
	}
	c.Next()
}

func identity(c *gin.Context) service.Identity {
	value, _ := c.Get("identity")
	return value.(service.Identity)
}

func writeResult(c *gin.Context, value any, err error) {
	if err != nil {
		writeError(c, http.StatusInternalServerError, err)
		return
	}
	c.JSON(http.StatusOK, value)
}

func writeError(c *gin.Context, status int, err error) {
	c.JSON(status, gin.H{"detail": gin.H{"error": err.Error()}})
}
