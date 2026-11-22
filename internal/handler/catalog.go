package handler

import (
	"net/http"
	"strconv"

	"github.com/basketikun/chatgpt2api/internal/config"
	"github.com/basketikun/chatgpt2api/internal/service"
	"github.com/gin-gonic/gin"
)

type CatalogHandler struct {
	service *service.CatalogService
	config  *config.Config
}

func NewCatalogHandler(service *service.CatalogService, config *config.Config) *CatalogHandler {
	return &CatalogHandler{service: service, config: config}
}

func (h *CatalogHandler) RegisterRoutes(router *gin.RouterGroup) { router.GET("/models", h.list) }

func (h *CatalogHandler) list(c *gin.Context) {
	refresh, _ := strconv.ParseBool(c.Query("refresh"))
	ids, err := h.service.List(c.Request.Context(), refresh)
	if err != nil {
		writeError(c, http.StatusBadGateway, err)
		return
	}
	data := make([]gin.H, 0, len(ids))
	for _, id := range ids {
		if !identity(c).AllowsModel(id) {
			continue
		}
		item := gin.H{"id": id, "object": "model", "created": 0, "owned_by": "chatgpt2api", "permission": []any{}, "root": id, "parent": nil}
		if service.IsImageModel(id, h.config.ImageModelName) {
			if _, err := service.ImageRoutes(id, identity(c)); err != nil {
				continue
			}
			item["type"] = "image"
			item["default"] = id == h.config.ImageModelName
		}
		data = append(data, item)
	}
	c.JSON(http.StatusOK, gin.H{"object": "list", "data": data})
}
