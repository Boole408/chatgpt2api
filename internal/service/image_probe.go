package service

import (
	"bytes"
	"context"
	"errors"
	"image"
	"time"

	_ "golang.org/x/image/webp"
)

var ErrImageProbeUnavailable = errors.New("账号状态、额度或并发限制不允许当前检测")
var ErrImageProbeInvalidRoute = errors.New("这个账号不支持指定的检测线路")
var ErrImageProbeFailed = errors.New("上游未返回可识别的图片")

type ImageProbeResult struct {
	Route  string `json:"route"`
	Format string `json:"format"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
}

func (s *AIService) ProbeImage(ctx context.Context, token, route string) (ImageProbeResult, error) {
	ctx, cancel := context.WithTimeout(ctx, 150*time.Second)
	defer cancel()
	account, err := s.accounts.Account(ctx, token)
	if err != nil {
		return ImageProbeResult{}, err
	}
	if route != "web" && (route != "codex" || !account.IsCodex) {
		return ImageProbeResult{}, ErrImageProbeInvalidRoute
	}
	accountID := account.ID
	acquired, err := s.accounts.repo.TryAcquireImageProbe(ctx, accountID, max(1, s.config.Image.AccountConcurrency))
	if err != nil {
		return ImageProbeResult{}, err
	}
	if !acquired {
		return ImageProbeResult{}, ErrImageProbeUnavailable
	}
	attempted, success := false, false
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if attempted {
			s.accounts.ReleaseImageToken(cleanup, token, success)
		} else {
			s.accounts.repo.ReleaseImageAccountByID(cleanup, accountID)
		}
	}()
	token, err = s.accounts.RefreshToken(ctx, account.AccessToken, false)
	if err != nil {
		return ImageProbeResult{}, ErrImageProbeFailed
	}
	if account, err = s.accounts.Account(ctx, token); err != nil {
		return ImageProbeResult{}, err
	}
	attempted = true
	request := ImageRequest{Prompt: "Draw a simple red circle on a white background.", Count: 1, Size: "1024x1024", Quality: "auto"}
	data, _, err := s.renderImage(ctx, account, request, route == "codex", nil, 0)
	if err != nil {
		return ImageProbeResult{}, ErrImageProbeFailed
	}
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return ImageProbeResult{}, ErrImageProbeFailed
	}
	success = true
	return ImageProbeResult{Route: route, Format: format, Width: cfg.Width, Height: cfg.Height}, nil
}
