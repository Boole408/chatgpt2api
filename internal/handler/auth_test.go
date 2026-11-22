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

func TestGeneratedUserKeyIsRevealedOnlyAtCreation(t *testing.T) {
	const admin = "fixture-admin"
	const path = "/api/auth/users"
	repo, err := repository.Open("sqlite:///" + filepath.Join(t.TempDir(), "auth.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()

	auth := service.NewAuthService(repo, admin)
	engine := gin.New()
	handler := NewAuthHandler(auth, "test")
	handler.RegisterLogin(engine)
	handler.RegisterRoutes(engine.Group("/api", Identity(auth), Admin))
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
		if created.Item.ID == "" || len(created.Items) == 0 || strings.Count(rec.Body.String(), created.Key) != 1 {
			t.Fatal("creation must reveal the key only at the top level")
		}
		stored, err := repo.APIKeyByID(context.Background(), created.Item.ID)
		if err != nil || stored.Key != repository.HashAPIKey(created.Key) {
			t.Fatal("expected only a hash in storage")
		}
		send("POST", "/auth/login", created.Key, "", http.StatusOK)
		send("POST", "/auth/login", stored.Key, "", http.StatusUnauthorized)
		send("GET", path, created.Key, "", http.StatusForbidden)

		updated := send("POST", path+"/"+created.Item.ID, admin, `{"name":"Edited `+created.Item.ID+`","key":""}`, http.StatusOK)
		listed := send("GET", path, admin, "", http.StatusOK)
		for _, rec := range []*httptest.ResponseRecorder{updated, listed} {
			out := decode(rec)
			if out.Key != "" || strings.Contains(rec.Body.String(), created.Key) || strings.Contains(rec.Body.String(), stored.Key) {
				t.Fatal("an existing generated key or its hash was exposed")
			}
		}
		send("POST", "/auth/login", created.Key, "", http.StatusOK)
	}

	const custom = "fixture-custom"
	customKey := decode(send("POST", path, admin, `{"key":"`+custom+`","plan":["free"],"route":["web"]}`, http.StatusOK))
	if customKey.Key != "" || customKey.Item.Key != custom {
		t.Fatal("custom key creation no longer returns the supplied key in its item")
	}
	send("POST", "/auth/login", custom, "", http.StatusOK)
}
