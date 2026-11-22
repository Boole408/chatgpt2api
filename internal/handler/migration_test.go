package handler

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/basketikun/chatgpt2api/internal/repository"
	"github.com/basketikun/chatgpt2api/internal/service"
	"github.com/gin-gonic/gin"
)

func TestMigratedKeyKeepsAuthorizationRules(t *testing.T) {
	const key = "migration-fixture-key"
	digest := sha256.Sum256([]byte(key))
	temp := t.TempDir()
	keys := filepath.Join(temp, "auth_keys.json")
	data := fmt.Sprintf(`{"items":[
		{"id":"old-user","role":"user","name":"Imported","key_hash":"%x","rate_limit_rpm":2,"allowed_models":["gpt-allowed*"],"expires_at":"2099-01-01T00:00:00Z"},
		{"id":"expired-user","role":"user","name":"Expired","key_hash":"%x","expires_at":"2000-01-01T00:00:00Z"}]}`,
		digest, sha256.Sum256([]byte("expired-fixture-key")))
	if err := os.WriteFile(keys, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	token := "fixture." + base64.RawURLEncoding.EncodeToString([]byte(`{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","https://api.openai.com/auth":{"chatgpt_plan_type":"plus","chatgpt_account_id":"fixture-account"},"https://api.openai.com/profile":{"email":"fixture@example.test"}}`)) + ".fixture"
	accounts := filepath.Join(temp, "accounts.json")
	if err := os.WriteFile(accounts, fmt.Appendf(nil, `[{"access_token":%q,"status":"禁用","quota":4,"refresh_token":"fixture-refresh"}]`, token), 0600); err != nil {
		t.Fatal(err)
	}
	snapshot, err := service.ReadLegacySnapshot(accounts, keys)
	if err != nil {
		t.Fatal(err)
	}
	db := "sqlite:///" + filepath.Join(temp, "go.db")
	repo, err := repository.Open(db)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := repo.InsertLegacySnapshot(ctx, snapshot.Accounts, snapshot.Keys); err != nil {
		t.Fatal(err)
	}
	if err := repo.Close(); err != nil {
		t.Fatal(err)
	}
	repo, err = repository.Open(db)
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()
	item, err := repo.AccountByEmail(ctx, "fixture@example.test")
	if err != nil || item.Type != "plus" || !item.IsCodex || item.Status != "禁用" || item.Quota != 4 || item.RefreshToken != "fixture-refresh" {
		t.Fatalf("account migration unsuccessful: %v", err)
	}
	auth := service.NewAuthService(repo, "fixture-admin")
	engine := gin.New()
	handler := NewAuthHandler(auth, "test")
	handler.RegisterLogin(engine)
	handler.RegisterRoutes(engine.Group("/api", Identity(auth), Admin))
	engine.POST("/fixture/:model", Identity(auth), LimitKeyRequests(auth), func(c *gin.Context) {
		if allowModel(c, c.Param("model")) {
			c.Status(http.StatusNoContent)
		}
	})
	send := func(method, path, key string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, nil)
		req.Header.Set("Authorization", "Bearer "+key)
		rec := httptest.NewRecorder()
		engine.ServeHTTP(rec, req)
		return rec
	}
	hash := hex.EncodeToString(digest[:])
	for _, check := range []struct {
		method, path, key string
		want              int
	}{
		{"POST", "/auth/login", key, 200},
		{"POST", "/auth/login", "expired-fixture-key", 401},
		{"POST", "/auth/login", "sha256:" + hash, 401},
		{"POST", "/fixture/gpt-denied", key, 403},
		{"POST", "/fixture/gpt-allowed-v1", key, 204},
		{"POST", "/fixture/gpt-allowed-v1", key, 429},
	} {
		response := send(check.method, check.path, check.key)
		if response.Code != check.want {
			t.Fatalf("%s %s = %d, want %d", check.method, check.path, response.Code, check.want)
		}
		if check.want == 429 && response.Header().Get("Retry-After") == "" {
			t.Fatal("rate limit missing Retry-After")
		}
	}
	response := send("GET", "/api/auth/users", "fixture-admin")
	if response.Code != 200 || strings.Contains(response.Body.String(), hash) || !strings.Contains(response.Body.String(), `"rate_limit_rpm":2`) {
		t.Fatal("list did not return key policy with digest hidden")
	}
}
