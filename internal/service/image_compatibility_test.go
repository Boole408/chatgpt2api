package service

import (
	"errors"
	"net/http"
	"testing"

	"github.com/basketikun/chatgpt2api/internal/config"
)

func TestLegacyImageRoutes(t *testing.T) {
	ai := &AIService{config: &config.Config{ImageModelName: "gpt-image-2.6"}}
	webKey := Identity{Role: "user", Routes: []string{"web"}}
	codexKey := Identity{Role: "user", Routes: []string{"codex"}}
	admin := Identity{Role: "admin", Routes: []string{"web"}}
	for _, test := range []struct {
		model, size string
		key         Identity
		wantStatus  int
	}{
		{"gpt-image-2.5", "1024x1024", webKey, 0},
		{"gpt-image-2.5", "1024X2048", webKey, http.StatusBadRequest},
		{"gpt-image-2", "2048x2048", webKey, http.StatusBadRequest},
		{"gpt-image-2", "1024x1024", codexKey, http.StatusForbidden},
		{"codex-gpt-image-2", "4096x4096", webKey, http.StatusForbidden},
		{"codex-gpt-image-2", "4096x4096", codexKey, 0},
		{"codex-gpt-image-2", "4096x4096", admin, 0},
		{"gpt-image-2.6", "2048x2048", admin, 0},
	} {
		err := ai.validateImage(ImageRequest{Model: test.model, Size: test.size}, test.key)
		if test.wantStatus == 0 && err == nil {
			continue
		}
		var imageError *ImageError
		if !errors.As(err, &imageError) || imageError.Status != test.wantStatus {
			t.Errorf("%s %s route %v: got %v, want status %d", test.model, test.size, test.key.Routes, err, test.wantStatus)
		}
	}
}
