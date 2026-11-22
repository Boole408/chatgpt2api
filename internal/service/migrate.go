package service

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/basketikun/chatgpt2api/internal/repository"
)

type legacyAccount struct {
	AccessToken   string  `json:"access_token"`
	RefreshToken  string  `json:"refresh_token"`
	IDToken       string  `json:"id_token"`
	Email         *string `json:"email"`
	UserID        *string `json:"user_id"`
	AccountID     string  `json:"account_id"`
	Status        string  `json:"status"`
	Quota         int     `json:"quota"`
	Proxy         string  `json:"proxy"`
	Success       int     `json:"success"`
	Fail          int     `json:"fail"`
	LastUsedAt    *string `json:"last_used_at"`
	LastRefreshAt *string `json:"last_refresh_at"`
	LastError     *string `json:"last_error"`
	CreatedAt     string  `json:"created_at"`
}

type legacyKey struct {
	ID            string     `json:"id"`
	Name          string     `json:"name"`
	Role          string     `json:"role"`
	KeyHash       string     `json:"key_hash"`
	Enabled       *bool      `json:"enabled"`
	CreatedAt     time.Time  `json:"created_at"`
	LastUsedAt    *time.Time `json:"last_used_at"`
	AllowedModels []string   `json:"allowed_models"`
	RateLimitRPM  int        `json:"rate_limit_rpm"`
	ExpiresAt     *time.Time `json:"expires_at"`
}

type LegacySnapshot struct {
	Accounts     []repository.Account
	Keys         []repository.APIKey
	SkippedAdmin int
}

func ReadLegacySnapshot(accountsPath, keysPath string) (LegacySnapshot, error) {
	var out LegacySnapshot
	now := time.Now().UTC()
	if accountsPath != "" {
		accounts, err := loadLegacyList[legacyAccount](accountsPath, "accounts")
		if err != nil {
			return out, err
		}
		emails := map[string]bool{}
		for i, old := range accounts {
			item := repository.Account{AccessToken: strings.TrimSpace(old.AccessToken), RefreshToken: old.RefreshToken, IDToken: old.IDToken, Email: old.Email, UserID: old.UserID, AccountID: old.AccountID, Status: old.Status, Quota: old.Quota, Proxy: old.Proxy, Success: old.Success, Fail: old.Fail, LastUsedAt: old.LastUsedAt, LastRefreshAt: old.LastRefreshAt, LastError: old.LastError, CreatedAt: old.CreatedAt}
			if err := applyTokenClaims(&item); err != nil || item.Email == nil || strings.TrimSpace(*item.Email) == "" {
				return out, fmt.Errorf("旧账号文件第 %d 条没有可导入的令牌或邮箱声明", i+1)
			}
			if emails[strings.ToLower(*item.Email)] {
				return out, fmt.Errorf("旧账号文件第 %d 条的邮箱重复；请先合并重复记录", i+1)
			}
			emails[strings.ToLower(*item.Email)] = true
			if item.CreatedAt == "" {
				item.CreatedAt = now.Format(time.RFC3339)
			}
			out.Accounts = append(out.Accounts, item)
		}
	}
	if keysPath == "" {
		return out, nil
	}
	keys, err := loadLegacyList[legacyKey](keysPath, "items")
	if err != nil {
		return out, err
	}
	for i, old := range keys {
		if old.Role == "admin" {
			out.SkippedAdmin++
			continue
		}
		hash, err := hex.DecodeString(old.KeyHash)
		if err != nil || len(hash) != 32 || old.Role != "user" || old.ID == "" || old.Name == "" {
			return out, fmt.Errorf("旧密钥文件第 %d 条不是有效的用户密钥", i+1)
		}
		if old.CreatedAt.IsZero() {
			old.CreatedAt = now
		}
		item := repository.APIKey{
			ID: old.ID, Name: old.Name, Role: old.Role, Key: repository.LegacyKeyFromHash(hash),
			Plan: "*", Route: "web,codex", Enabled: old.Enabled == nil || *old.Enabled,
			CreatedAt: old.CreatedAt, LastUsedAt: old.LastUsedAt,
		}
		policy := KeyPolicy{AllowedModels: old.AllowedModels, RateLimitRPM: old.RateLimitRPM, ExpiresAt: old.ExpiresAt}
		if err := policy.Apply(&item); err != nil {
			return out, fmt.Errorf("旧密钥文件第 %d 条的限制设置无法导入：%w", i+1, err)
		}
		out.Keys = append(out.Keys, item)
	}
	return out, nil
}

func loadLegacyList[T any](path, field string) ([]T, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var rows []T
	if err := json.Unmarshal(data, &rows); err == nil && rows != nil {
		return rows, nil
	}
	var wrapped map[string]json.RawMessage
	if json.Unmarshal(data, &wrapped) == nil && json.Unmarshal(wrapped[field], &rows) == nil && rows != nil {
		return rows, nil
	}
	return nil, fmt.Errorf("%s 应包含 JSON 列表或 %q 字段", path, field)
}
