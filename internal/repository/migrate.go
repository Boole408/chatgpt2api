package repository

import (
	"context"
	"errors"
	"fmt"

	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// InsertLegacySnapshot only adds records. An existing account or key rolls back
// the entire import, so an old snapshot can't overwrite refreshed credentials.
func (r *Repository) InsertLegacySnapshot(ctx context.Context, accounts []Account, keys []APIKey) error {
	db := r.db.WithContext(ctx).Session(&gorm.Session{Logger: logger.Default.LogMode(logger.Silent)})
	return db.Transaction(func(tx *gorm.DB) error {
		for index := range accounts {
			if err := tx.Create(&accounts[index]).Error; err != nil {
				return fmt.Errorf("账号 %d 写入失败：请检查该邮箱是否已经存在，以及数据库是否可写", index+1)
			}
		}
		for index := range keys {
			var existing []APIKey
			if err := tx.Select("key").Where("role = ?", "user").Find(&existing).Error; err != nil {
				return errors.New("无法检查目标库的 API 密钥")
			}
			for _, key := range existing {
				if IsLegacyKey(key.Key) {
					continue
				}
				if legacyKeyValue(key.Key) == keys[index].Key {
					return fmt.Errorf("用户密钥 %d 已存在于目标库", index+1)
				}
			}
			if err := tx.Create(&keys[index]).Error; err != nil {
				return fmt.Errorf("用户密钥 %d 写入失败：请检查该密钥或名称是否已经存在，以及数据库是否可写", index+1)
			}
		}
		return nil
	})
}
