package handler

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/basketikun/chatgpt2api/internal/repository"
	"github.com/basketikun/chatgpt2api/internal/service"
	"github.com/gin-gonic/gin"
)

type AuthHandler struct {
	service *service.AuthService
	version string
}

func NewAuthHandler(service *service.AuthService, version string) *AuthHandler {
	return &AuthHandler{service: service, version: version}
}

func (h *AuthHandler) RegisterLogin(router *gin.Engine) { router.POST("/auth/login", h.login) }

func (h *AuthHandler) RegisterRoutes(router *gin.RouterGroup) {
	keys := router.Group("/auth/users", func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.Next()
	})
	keys.GET("", h.list)
	keys.POST("", h.create)
	keys.GET("/:keyID/secret", h.reveal)
	keys.POST("/:keyID", h.update)
	keys.DELETE("/:keyID", h.delete)
}

func (h *AuthHandler) login(c *gin.Context) {
	identity, err := h.service.Authenticate(c.Request.Context(), c.GetHeader("Authorization"), true)
	if err != nil {
		writeError(c, http.StatusUnauthorized, service.ErrUnauthorized)
		return
	}
	c.JSON(http.StatusOK, gin.H{"ok": true, "version": h.version, "role": identity.Role, "subject_id": identity.ID, "name": identity.Name})
}

func (h *AuthHandler) list(c *gin.Context) {
	items, err := h.service.List(c.Request.Context())
	writeResult(c, gin.H{"items": toAPIKeyResponses(items)}, err)
}

func (h *AuthHandler) reveal(c *gin.Context) {
	key, err := h.service.Reveal(c.Request.Context(), c.Param("keyID"))
	switch {
	case errors.Is(err, repository.ErrNotFound):
		writeError(c, http.StatusNotFound, errors.New("这条用户密钥不存在"))
	case errors.Is(err, service.ErrKeyNotRecoverable):
		writeError(c, http.StatusConflict, err)
	default:
		writeResult(c, gin.H{"key": key}, err)
	}
}

func (h *AuthHandler) create(c *gin.Context) {
	var body CreateAPIKeyRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		writeError(c, http.StatusBadRequest, errors.New("请选择有效的订阅等级和线路"))
		return
	}
	// Read the list before writing so a later read error cannot strand a
	// newly generated secret that has never been sent to the administrator.
	items, err := h.service.List(c.Request.Context())
	if err != nil {
		writeResult(c, nil, err)
		return
	}
	item, generated, err := h.service.Create(c.Request.Context(), body.Name, body.Key, body.Plan, body.Route, body.Policy)
	if err != nil {
		writeError(c, http.StatusBadRequest, err)
		return
	}
	result := gin.H{"item": toAPIKeyResponse(item), "items": toAPIKeyResponses(append(items, item))}
	if generated != "" {
		result["key"] = generated
	}
	c.JSON(http.StatusOK, result)
}

func (h *AuthHandler) update(c *gin.Context) {
	var body UpdateAPIKeyRequest
	if err := c.ShouldBindJSON(&body); err != nil || body.Name == nil && body.Enabled == nil && body.Key == nil && len(body.Plan) == 0 && len(body.Route) == 0 && body.Policy == nil {
		writeError(c, http.StatusBadRequest, errors.New("还没有检测到改动，请修改后再保存"))
		return
	}
	item, err := h.service.Update(c.Request.Context(), c.Param("keyID"), body.Name, body.Enabled, body.Key, body.Plan, body.Route, body.Policy)
	if errors.Is(err, repository.ErrNotFound) {
		writeError(c, http.StatusNotFound, errors.New("这条用户密钥不存在，可能已经被删除"))
		return
	}
	if err != nil {
		writeError(c, http.StatusBadRequest, err)
		return
	}
	items, err := h.service.List(c.Request.Context())
	writeResult(c, gin.H{"item": toAPIKeyResponse(item), "items": toAPIKeyResponses(items)}, err)
}

func (h *AuthHandler) delete(c *gin.Context) {
	err := h.service.Delete(c.Request.Context(), c.Param("keyID"))
	if errors.Is(err, repository.ErrNotFound) {
		writeError(c, http.StatusNotFound, errors.New("这条用户密钥不存在，可能已经被删除"))
		return
	}
	items, listErr := h.service.List(c.Request.Context())
	if err == nil {
		err = listErr
	}
	writeResult(c, gin.H{"items": toAPIKeyResponses(items)}, err)
}

func toAPIKeyResponse(item repository.APIKey) APIKeyResponse {
	models := []string{}
	if item.AllowedModels != "" {
		models = strings.Split(item.AllowedModels, ",")
	}
	return APIKeyResponse{ID: item.ID, Name: item.Name, Role: item.Role, CanReveal: item.CanReveal(), Plan: strings.Split(item.Plan, ","), Route: strings.Split(item.Route, ","), Enabled: item.Enabled, CreatedAt: item.CreatedAt, LastUsedAt: item.LastUsedAt, Policy: service.KeyPolicy{AllowedModels: models, RateLimitRPM: item.RateLimitRPM, ExpiresAt: item.ExpiresAt}}
}

func toAPIKeyResponses(items []repository.APIKey) []APIKeyResponse {
	result := make([]APIKeyResponse, 0, len(items))
	for _, item := range items {
		result = append(result, toAPIKeyResponse(item))
	}
	return result
}

type APIKeyResponse struct {
	ID         string            `json:"id"`
	Name       string            `json:"name"`
	Role       string            `json:"role"`
	CanReveal  bool              `json:"can_reveal"`
	Plan       []string          `json:"plan"`
	Route      []string          `json:"route"`
	Enabled    bool              `json:"enabled"`
	CreatedAt  time.Time         `json:"created_at"`
	LastUsedAt *time.Time        `json:"last_used_at"`
	Policy     service.KeyPolicy `json:"policy"`
}
type CreateAPIKeyRequest struct {
	Name   string             `json:"name"`
	Key    string             `json:"key"`
	Plan   []string           `json:"plan" binding:"required,min=1,dive,oneof=* free plus team pro"`
	Route  []string           `json:"route" binding:"required,min=1,dive,oneof=web codex"`
	Policy *service.KeyPolicy `json:"policy"`
}
type UpdateAPIKeyRequest struct {
	Name    *string            `json:"name"`
	Enabled *bool              `json:"enabled"`
	Key     *string            `json:"key"`
	Plan    []string           `json:"plan" binding:"omitempty,dive,oneof=* free plus team pro"`
	Route   []string           `json:"route" binding:"omitempty,dive,oneof=web codex"`
	Policy  *service.KeyPolicy `json:"policy"`
}
