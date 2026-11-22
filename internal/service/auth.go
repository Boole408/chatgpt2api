package service

import (
	"context"
	"crypto/cipher"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"path"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/basketikun/chatgpt2api/internal/repository"
	"github.com/google/uuid"
)

var ErrUnauthorized = errors.New("密钥无效或已失效，请重新登录")
var ErrKeyNotRecoverable = errors.New("此密钥只保存了哈希，请编辑并填入原密钥或新的密钥")

type Identity struct {
	ID            string
	Name          string
	Role          string
	Plans         []string
	Routes        []string
	AllowedModels []string
	RateLimitRPM  int
}

type AuthService struct {
	repo      *repository.Repository
	adminKey  string
	vault     cipher.AEAD
	mu        sync.RWMutex
	keys      map[string]cachedKey
	windows   map[string][]time.Time
	lastSweep time.Time
}

type cachedKey struct {
	item      repository.APIKey
	expiresAt time.Time
}

func NewAuthService(repo *repository.Repository, adminKey, dataDir string) (*AuthService, error) {
	vault, err := openAccessKeyVault(dataDir)
	if err != nil {
		return nil, err
	}
	return &AuthService{repo: repo, adminKey: adminKey, vault: vault, keys: map[string]cachedKey{}, windows: map[string][]time.Time{}}, nil
}

func (s *AuthService) apiKey(ctx context.Context, key string) (repository.APIKey, error) {
	s.mu.RLock()
	cached, ok := s.keys[key]
	s.mu.RUnlock()
	if ok && time.Now().Before(cached.expiresAt) {
		return cached.item, nil
	}
	item, err := s.repo.APIKeyByValue(ctx, key)
	if err != nil {
		return item, err
	}
	s.mu.Lock()
	s.keys[key] = cachedKey{item: item, expiresAt: time.Now().Add(30 * time.Second)}
	s.mu.Unlock()
	return item, nil
}

func (s *AuthService) clearKeys() {
	s.mu.Lock()
	clear(s.keys)
	s.mu.Unlock()
}

func (s *AuthService) Authenticate(ctx context.Context, authorization string, touch bool) (Identity, error) {
	scheme, key, ok := strings.Cut(authorization, " ")
	if !ok || !strings.EqualFold(scheme, "bearer") || key == "" {
		return Identity{}, ErrUnauthorized
	}
	if subtle.ConstantTimeCompare([]byte(key), []byte(s.adminKey)) == 1 {
		return Identity{ID: "admin", Name: "管理员", Role: "admin", Routes: []string{"web"}}, nil
	}
	item, err := s.apiKey(ctx, key)
	if errors.Is(err, repository.ErrNotFound) {
		return Identity{}, ErrUnauthorized
	}
	if err != nil {
		return Identity{}, err
	}
	if item.ExpiresAt != nil && !time.Now().Before(*item.ExpiresAt) {
		return Identity{}, ErrUnauthorized
	}
	if touch {
		if err := s.repo.TouchAPIKey(ctx, item.ID, time.Now().UTC()); err != nil {
			return Identity{}, err
		}
	}
	var patterns []string
	if item.AllowedModels != "" {
		patterns = strings.Split(item.AllowedModels, ",")
	}
	var plans []string
	if item.Plan != "*" && item.Plan != "" {
		plans = strings.Split(item.Plan, ",")
	}
	return Identity{ID: item.ID, Name: item.Name, Role: item.Role, Plans: plans, Routes: strings.Split(item.Route, ","), AllowedModels: patterns, RateLimitRPM: item.RateLimitRPM}, nil
}

func (i Identity) AllowsModel(model string) bool {
	if i.Role == "admin" || len(i.AllowedModels) == 0 {
		return true
	}
	model = strings.ToLower(strings.TrimSpace(model))
	names := []string{model}
	for _, effort := range []string{"-standard", "-extended", "-max"} {
		if strings.HasSuffix(model, effort) {
			names = append(names, strings.TrimSuffix(model, effort))
		}
	}
	for _, pattern := range i.AllowedModels {
		for _, name := range names {
			if ok, _ := path.Match(strings.ToLower(pattern), name); ok {
				return true
			}
		}
	}
	return false
}

func (s *AuthService) RetryAfter(identity Identity, now time.Time) int {
	if identity.Role == "admin" || identity.RateLimitRPM <= 0 {
		return 0
	}
	cutoff := now.Add(-time.Minute)
	s.mu.Lock()
	defer s.mu.Unlock()
	if now.Sub(s.lastSweep) >= time.Minute {
		for id, window := range s.windows {
			if len(window) == 0 || !window[len(window)-1].After(cutoff) {
				delete(s.windows, id)
			}
		}
		s.lastSweep = now
	}
	window := s.windows[identity.ID]
	for len(window) > 0 && !window[0].After(cutoff) {
		window = window[1:]
	}
	if len(window) >= identity.RateLimitRPM {
		s.windows[identity.ID] = window
		return max(1, int((window[0].Sub(cutoff)+time.Second-1)/time.Second))
	}
	s.windows[identity.ID] = append(window, now)
	return 0
}

func (s *AuthService) List(ctx context.Context) ([]repository.APIKey, error) {
	return s.repo.APIKeys(ctx, "user")
}

func (s *AuthService) Reveal(ctx context.Context, id string) (string, error) {
	item, err := s.repo.APIKeyByID(ctx, id)
	if err != nil {
		return "", err
	}
	if item.Role != "user" {
		return "", repository.ErrNotFound
	}
	if !item.CanReveal() {
		return "", ErrKeyNotRecoverable
	}
	return s.unprotectKey(item)
}

type KeyPolicy struct {
	AllowedModels []string   `json:"allowed_models"`
	RateLimitRPM  int        `json:"rate_limit_rpm"`
	ExpiresAt     *time.Time `json:"expires_at"`
}

func (p *KeyPolicy) Apply(item *repository.APIKey) error {
	if p == nil {
		return nil
	}
	if p.RateLimitRPM < 0 || p.RateLimitRPM > 100_000 {
		return errors.New("每分钟请求上限须在 0 至 100000 之间")
	}
	models := make([]string, 0, len(p.AllowedModels))
	for _, pattern := range p.AllowedModels {
		pattern = strings.TrimSpace(pattern)
		if pattern == "" {
			continue
		}
		if _, err := path.Match(pattern, ""); err != nil || strings.Contains(pattern, ",") {
			return errors.New("模型白名单包含格式不正确的模式")
		}
		models = append(models, pattern)
	}
	item.AllowedModels, item.RateLimitRPM, item.ExpiresAt = strings.Join(models, ","), p.RateLimitRPM, p.ExpiresAt
	return nil
}

func keyPlans(plan []string) string {
	if slices.Contains(plan, "*") {
		return "*"
	}
	return strings.Join(plan, ",")
}

func (s *AuthService) Create(ctx context.Context, name string, key string, plan []string, route []string, policy *KeyPolicy) (repository.APIKey, string, error) {
	generated := ""
	if strings.TrimSpace(key) == "" {
		var secret [32]byte
		if _, err := rand.Read(secret[:]); err != nil {
			return repository.APIKey{}, "", errors.New("无法生成密钥，请重试")
		}
		key = "sk-" + base64.RawURLEncoding.EncodeToString(secret[:])
		generated = key
	}
	if repository.IsHashedAPIKey(key) {
		return repository.APIKey{}, "", errors.New("请输入新的专用密钥")
	}
	if len(key) > 255 {
		return repository.APIKey{}, "", errors.New("密钥长度不能超过 255 字节")
	}
	if key == s.adminKey {
		return repository.APIKey{}, "", errors.New("这个密钥和管理员密钥冲突了，请换一个新的密钥")
	}
	exists, err := s.repo.APIKeyExists(ctx, key, "")
	if err != nil {
		return repository.APIKey{}, "", err
	}
	if exists {
		return repository.APIKey{}, "", errors.New("这个专用密钥已经存在，请换一个新的密钥")
	}
	name, err = s.availableName(ctx, name, "")
	if err != nil {
		return repository.APIKey{}, "", err
	}
	item := repository.APIKey{ID: strings.ReplaceAll(uuid.NewString(), "-", ""), Name: name, Role: "user", Plan: keyPlans(plan), Route: strings.Join(route, ","), Enabled: true, CreatedAt: time.Now().UTC()}
	if err := policy.Apply(&item); err != nil {
		return repository.APIKey{}, "", err
	}
	s.protectKey(&item, key)
	if err := s.repo.SaveAPIKey(ctx, item); err != nil {
		return repository.APIKey{}, "", err
	}
	return item, generated, nil
}

func (s *AuthService) Update(ctx context.Context, id string, name *string, enabled *bool, key *string, plan []string, route []string, policy *KeyPolicy) (repository.APIKey, error) {
	item, err := s.repo.APIKeyByID(ctx, id)
	if err != nil {
		return repository.APIKey{}, err
	}
	if item.Role != "user" {
		return repository.APIKey{}, repository.ErrNotFound
	}
	if name != nil {
		item.Name, err = s.availableName(ctx, *name, id)
		if err != nil {
			return repository.APIKey{}, err
		}
	}
	if key != nil && strings.TrimSpace(*key) != "" {
		if *key == s.adminKey || repository.IsHashedAPIKey(*key) {
			return repository.APIKey{}, errors.New("请输入与管理员密钥不同的新密钥")
		}
		if len(*key) > 255 {
			return repository.APIKey{}, errors.New("密钥长度不能超过 255 字节")
		}
		exists, err := s.repo.APIKeyExists(ctx, *key, id)
		if err != nil {
			return repository.APIKey{}, err
		}
		if exists {
			return repository.APIKey{}, errors.New("这个专用密钥已经存在，请换一个新的密钥")
		}
		s.protectKey(&item, *key)
	}
	if enabled != nil {
		item.Enabled = *enabled
	}
	if len(plan) > 0 {
		item.Plan = keyPlans(plan)
	}
	if len(route) > 0 {
		item.Route = strings.Join(route, ",")
	}
	if err := policy.Apply(&item); err != nil {
		return repository.APIKey{}, err
	}
	err = s.repo.SaveAPIKey(ctx, item)
	s.clearKeys()
	return item, err
}

func (s *AuthService) Delete(ctx context.Context, id string) error {
	item, err := s.repo.APIKeyByID(ctx, id)
	if err != nil {
		return err
	}
	if item.Role != "user" {
		return repository.ErrNotFound
	}
	err = s.repo.DeleteAPIKey(ctx, id)
	s.clearKeys()
	return err
}

func (s *AuthService) availableName(ctx context.Context, name string, excludeID string) (string, error) {
	if name != "" {
		exists, err := s.repo.APIKeyNameExists(ctx, name, "user", excludeID)
		if err != nil {
			return "", err
		}
		if exists {
			return "", errors.New("这个名称已经在使用中了，换一个更容易区分的名称吧")
		}
		return name, nil
	}
	name = "普通用户"
	for suffix := 2; ; suffix++ {
		exists, err := s.repo.APIKeyNameExists(ctx, name, "user", excludeID)
		if err != nil || !exists {
			return name, err
		}
		name = "普通用户 " + strconv.Itoa(suffix)
	}
}
