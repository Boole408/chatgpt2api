package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"gorm.io/gorm"
)

const legacyKeyPrefix = "sha256:"

func legacyKeyValue(key string) string {
	digest := sha256.Sum256([]byte(key))
	return legacyKeyPrefix + hex.EncodeToString(digest[:])
}

func IsLegacyKey(value string) bool { return strings.HasPrefix(value, legacyKeyPrefix) }

func LegacyKeyFromHash(digest []byte) string { return legacyKeyPrefix + hex.EncodeToString(digest) }

type APIKey struct {
	ID            string    `gorm:"type:varchar(32);primaryKey"`
	Name          string    `gorm:"type:varchar(255);not null;uniqueIndex:api_key_role_name"`
	Role          string    `gorm:"type:varchar(16);not null;uniqueIndex:api_key_role_name"`
	Key           string    `gorm:"type:varchar(255);not null;uniqueIndex"`
	Plan          string    `gorm:"type:varchar(32);not null;default:free"`
	Route         string    `gorm:"type:varchar(32);not null;default:web"`
	Enabled       bool      `gorm:"not null"`
	CreatedAt     time.Time `gorm:"not null"`
	LastUsedAt    *time.Time
	AllowedModels string `gorm:"type:text"`
	RateLimitRPM  int    `gorm:"not null;default:0"`
	ExpiresAt     *time.Time
}

func (APIKey) TableName() string { return "api_keys" }

func (r *Repository) APIKeyByValue(ctx context.Context, key string) (APIKey, error) {
	var item APIKey
	if IsLegacyKey(key) {
		return item, ErrNotFound
	}
	if err := r.db.WithContext(ctx).Where("key IN ? AND enabled = ?", []string{key, legacyKeyValue(key)}, true).First(&item).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return APIKey{}, ErrNotFound
		}
		return APIKey{}, err
	}
	return item, nil
}

func (r *Repository) TouchAPIKey(ctx context.Context, id string, usedAt time.Time) error {
	return r.db.WithContext(ctx).Model(&APIKey{}).Where("id = ?", id).Update("last_used_at", usedAt).Error
}

func (r *Repository) APIKeys(ctx context.Context, role string) ([]APIKey, error) {
	var items []APIKey
	err := r.db.WithContext(ctx).Where("role = ?", role).Order("created_at").Find(&items).Error
	return items, err
}

func (r *Repository) APIKeyByID(ctx context.Context, id string) (APIKey, error) {
	var item APIKey
	if err := r.db.WithContext(ctx).Where("id = ?", id).First(&item).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return APIKey{}, ErrNotFound
		}
		return APIKey{}, err
	}
	return item, nil
}

func (r *Repository) APIKeyExists(ctx context.Context, key string, excludeID string) (bool, error) {
	db := r.db.WithContext(ctx).Model(&APIKey{}).Where("key IN ?", []string{key, legacyKeyValue(key)})
	if excludeID != "" {
		db = db.Where("id <> ?", excludeID)
	}
	var count int64
	err := db.Count(&count).Error
	return count > 0, err
}

func (r *Repository) APIKeyNameExists(ctx context.Context, name string, role string, excludeID string) (bool, error) {
	db := r.db.WithContext(ctx).Model(&APIKey{}).Where("name = ? AND role = ?", name, role)
	if excludeID != "" {
		db = db.Where("id <> ?", excludeID)
	}
	var count int64
	err := db.Count(&count).Error
	return count > 0, err
}

func (r *Repository) SaveAPIKey(ctx context.Context, item APIKey) error {
	return r.db.WithContext(ctx).Save(&item).Error
}

func (r *Repository) DeleteAPIKey(ctx context.Context, id string) error {
	return r.db.WithContext(ctx).Delete(&APIKey{}, "id = ?", id).Error
}
