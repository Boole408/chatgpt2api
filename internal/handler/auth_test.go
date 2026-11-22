package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/basketikun/chatgpt2api/internal/repository"
	"github.com/basketikun/chatgpt2api/internal/service"
	"github.com/gin-gonic/gin"
)

func TestUserKeyCanBeRevealedByAdminAfterCreation(t *testing.T) {
	admin := "fixture-admin"
	const path = "/api/auth/users"
	dir := t.TempDir()
	ctx := context.Background()
	repo, err := repository.Open("sqlite:///" + filepath.Join(dir, "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()

	newRouter := func() *gin.Engine {
		t.Helper()
		auth, err := service.NewAuthService(repo, admin, dir)
		if err != nil {
			t.Fatal(err)
		}
		engine := gin.New()
		handler := NewAuthHandler(auth, "test")
		handler.RegisterLogin(engine)
		handler.RegisterRoutes(engine.Group("/api", Identity(auth), Admin))
		return engine
	}
	engine := newRouter()
	send := func(method, path, key, body string, want int) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+key)
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		engine.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Fatalf("%s %s = %d, want %d", method, path, rec.Code, want)
		}
		return rec
	}
	type response struct {
		Key   string           `json:"key"`
		Item  APIKeyResponse   `json:"item"`
		Items []APIKeyResponse `json:"items"`
	}
	decode := func(rec *httptest.ResponseRecorder) response {
		t.Helper()
		if rec.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("key management response must not be cached")
		}
		var out response
		if json.Unmarshal(rec.Body.Bytes(), &out) != nil {
			t.Fatal("could not decode key management response")
		}
		return out
	}

	var firstKey string
	for _, body := range []string{
		`{"plan":["*"],"route":["web","codex"]}`,
		`{"key":"  ","plan":["*"],"route":["web","codex"]}`,
	} {
		rec := send("POST", path, admin, body, http.StatusOK)
		created := decode(rec)
		encoded, ok := strings.CutPrefix(created.Key, "sk-")
		bytes, err := base64.RawURLEncoding.DecodeString(encoded)
		if !ok || err != nil || len(bytes) != 32 || created.Key == firstKey {
			t.Fatal("expected a fresh generated key")
		}
		firstKey = created.Key
		if created.Item.ID == "" || !created.Item.CanReveal || len(created.Items) == 0 || strings.Count(rec.Body.String(), created.Key) != 1 {
			t.Fatal("creation must return the item and preserve the top-level key response")
		}
		stored, err := repo.APIKeyByID(ctx, created.Item.ID)
		if err != nil || stored.Key != repository.HashAPIKey(created.Key) || stored.EncryptedKey == "" || strings.Contains(stored.EncryptedKey, created.Key) {
			t.Fatal("expected a hash and an encrypted copy in storage")
		}
		secretPath := path + "/" + created.Item.ID + "/secret"
		send("POST", "/auth/login", created.Key, "", http.StatusOK)
		send("POST", "/auth/login", stored.Key, "", http.StatusUnauthorized)
		send("POST", "/auth/login", stored.EncryptedKey, "", http.StatusUnauthorized)
		send("GET", path, created.Key, "", http.StatusForbidden)
		send("GET", secretPath, created.Key, "", http.StatusForbidden)
		send("GET", secretPath, "", "", http.StatusUnauthorized)

		updated := send("POST", path+"/"+created.Item.ID, admin, `{"name":"Edited `+created.Item.ID+`","key":""}`, http.StatusOK)
		listed := send("GET", path, admin, "", http.StatusOK)
		for _, rec := range []*httptest.ResponseRecorder{updated, listed} {
			out := decode(rec)
			if out.Key != "" || strings.Contains(rec.Body.String(), created.Key) || strings.Contains(rec.Body.String(), stored.Key) || strings.Contains(rec.Body.String(), stored.EncryptedKey) {
				t.Fatal("metadata response exposed key material")
			}
		}
		if key := decode(send("GET", secretPath, admin, "", http.StatusOK)).Key; key != created.Key {
			t.Fatal("administrator did not receive the selected key")
		}
		// A new process and a different admin password still use the persisted vault key.
		admin += "-rotated"
		engine = newRouter()
		if key := decode(send("GET", secretPath, admin, "", http.StatusOK)).Key; key != created.Key {
			t.Fatal("key could not be recovered after reopening the vault")
		}
		send("POST", "/auth/login", created.Key, "", http.StatusOK)
	}

	const custom = "fixture-custom"
	customKey := decode(send("POST", path, admin, `{"key":"`+custom+`","plan":["free"],"route":["web"]}`, http.StatusOK))
	if customKey.Key != "" || !customKey.Item.CanReveal || decode(send("GET", path+"/"+customKey.Item.ID+"/secret", admin, "", http.StatusOK)).Key != custom {
		t.Fatal("custom keys are not available for on-demand copy")
	}
	send("POST", "/auth/login", custom, "", http.StatusOK)

	const legacy = "fixture-old-plaintext"
	const hashed = "fixture-old-hashed"
	for _, item := range []repository.APIKey{
		{ID: "plaintext", Name: "Old plaintext", Role: "user", Key: legacy, Enabled: true},
		{ID: "hashed", Name: "Old hash", Role: "user", Key: repository.HashAPIKey(hashed), Enabled: true},
	} {
		if err := repo.SaveAPIKey(ctx, item); err != nil {
			t.Fatal(err)
		}
	}
	rec := send("GET", path, admin, "", http.StatusOK)
	if strings.Contains(rec.Body.String(), legacy) {
		t.Fatal("key list revealed a legacy plaintext key")
	}
	if decode(send("GET", path+"/plaintext/secret", admin, "", http.StatusOK)).Key != legacy {
		t.Fatal("legacy plaintext key was not readable on demand")
	}
	send("POST", "/auth/login", hashed, "", http.StatusOK)
	send("GET", path+"/hashed/secret", admin, "", http.StatusConflict)
	restored := decode(send("POST", path+"/hashed", admin, `{"key":"`+hashed+`"}`, http.StatusOK))
	if !restored.Item.CanReveal || decode(send("GET", path+"/hashed/secret", admin, "", http.StatusOK)).Key != hashed {
		t.Fatal("re-entering the original key did not make it available for copy")
	}
	send("POST", "/auth/login", hashed, "", http.StatusOK)
	const replacement = "fixture-replacement"
	send("POST", path+"/hashed", admin, `{"key":"`+replacement+`"}`, http.StatusOK)
	send("POST", "/auth/login", hashed, "", http.StatusUnauthorized)
	send("POST", "/auth/login", replacement, "", http.StatusOK)
	if decode(send("GET", path+"/hashed/secret", admin, "", http.StatusOK)).Key != replacement {
		t.Fatal("replacement was not available for copy")
	}
}
