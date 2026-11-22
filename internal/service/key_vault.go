package service

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/basketikun/chatgpt2api/internal/repository"
)

const accessKeyMasterFile = "access-keys.aes"

func openAccessKeyVault(dataDir string) (cipher.AEAD, error) {
	path := filepath.Join(dataDir, accessKeyMasterFile)
	key, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		key, err = createAccessKeyMaster(path)
	}
	if err != nil {
		return nil, fmt.Errorf("读取访问密钥加密文件失败: %w", err)
	}
	if len(key) != 32 {
		return nil, errors.New("访问密钥加密文件无效")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCMWithRandomNonce(block)
}

func createAccessKeyMaster(path string) ([]byte, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp(dir, ".access-keys-*")
	if err != nil {
		return nil, err
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()

	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	if _, err := tmp.Write(key); err != nil {
		return nil, err
	}
	if err := tmp.Sync(); err != nil {
		return nil, err
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}
	// Publish the fully written file without overwriting another process's key.
	if err := os.Link(tmp.Name(), path); errors.Is(err, os.ErrExist) {
		return os.ReadFile(path)
	} else if err != nil {
		return nil, err
	}
	return key, nil
}

func (s *AuthService) protectKey(item *repository.APIKey, plain string) {
	item.Key = repository.HashAPIKey(plain)
	item.EncryptedKey = base64.RawStdEncoding.EncodeToString(s.vault.Seal(nil, nil, []byte(plain), keyContext(*item)))
}

func (s *AuthService) unprotectKey(item repository.APIKey) (string, error) {
	if !repository.IsHashedAPIKey(item.Key) {
		return item.Key, nil
	}
	ciphertext, err := base64.RawStdEncoding.DecodeString(item.EncryptedKey)
	if err == nil {
		if plain, err := s.vault.Open(nil, nil, ciphertext, keyContext(item)); err == nil {
			return string(plain), nil
		}
	}
	return "", errors.New("无法解密此密钥，请检查加密文件或重新填写密钥")
}

func keyContext(item repository.APIKey) []byte { return []byte(item.ID + ":" + item.Key) }
