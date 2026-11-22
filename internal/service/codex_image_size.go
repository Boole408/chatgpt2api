package service

import (
	"bytes"
	"context"
	"errors"
	"image"
	"image/draw"
	"image/png"
	"math"
	"regexp"
	"strconv"
	"strings"

	"github.com/disintegration/imaging"
)

var exactCodexSize = regexp.MustCompile(`(?i)^([0-9]{1,5})x([0-9]{1,5})$`)
var codexImageResizeSlot = make(chan struct{}, 1)

var errInvalidCodexSize = errors.New("Codex 图片尺寸必须为正，最长边不得超过 8192，总像素不得超过 4000 万")
var errInvalidCodexImage = errors.New("Codex 返回了无法处理的图片")

func codexSize(size string) (w, h int, err error) {
	match := exactCodexSize.FindStringSubmatch(strings.TrimSpace(size))
	if match == nil {
		return 0, 0, nil
	}
	w, _ = strconv.Atoi(match[1])
	h, _ = strconv.Atoi(match[2])
	if min(w, h) <= 0 || max(w, h) > 8192 || w*h > 40_000_000 {
		return 0, 0, errInvalidCodexSize
	}
	return w, h, nil
}

// The Codex tool can return a nearby canvas. Match the legacy API's exact
// output size without scaling or stretching anything on the web route.
func normalizeCodexImage(ctx context.Context, data []byte, size string, callLog *CallLog) ([]byte, error) {
	w, h, err := codexSize(size)
	if err != nil || w == 0 {
		return data, err
	}
	cfg, _, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || min(cfg.Width, cfg.Height) <= 0 || max(cfg.Width, cfg.Height) > 8192 || int64(cfg.Width)*int64(cfg.Height) > 40_000_000 {
		return nil, errInvalidCodexImage
	}
	if cfg.Width == w && cfg.Height == h {
		return data, nil
	}
	select {
	case codexImageResizeSlot <- struct{}{}:
		defer func() { <-codexImageResizeSlot }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	src, err := imaging.Decode(bytes.NewReader(data), imaging.AutoOrientation(true))
	if err != nil {
		return nil, errInvalidCodexImage
	}
	sw, sh := src.Bounds().Dx(), src.Bounds().Dy()
	originalAspect, targetAspect := float64(sw)/float64(sh), float64(w)/float64(h)
	retained := min(originalAspect, targetAspect) / max(originalAspect, targetAspect)
	var normalized *image.NRGBA
	strategy := "center_crop"
	if retained >= 0.92 {
		normalized = imaging.Fill(src, w, h, imaging.Center, imaging.Lanczos)
	} else {
		// For a large aspect change, preserve the whole subject over a
		// blurred extension instead of cropping it away.
		strategy = "blurred_canvas"
		previewScale := 400.0 / float64(max(w, h))
		bw, bh := max(1, int(math.Round(float64(w)*previewScale))), max(1, int(math.Round(float64(h)*previewScale)))
		background := imaging.Blur(imaging.Fill(src, bw, bh, imaging.Center, imaging.Linear), 5)
		normalized = imaging.Resize(background, w, h, imaging.Linear)
		scale := min(float64(w)/float64(sw), float64(h)/float64(sh))
		fw, fh := min(w, max(1, int(math.Round(float64(sw)*scale)))), min(h, max(1, int(math.Round(float64(sh)*scale))))
		foreground := imaging.Resize(src, fw, fh, imaging.Lanczos)
		offset := image.Pt((w-fw)/2, (h-fh)/2)
		draw.Draw(normalized, foreground.Bounds().Add(offset), foreground, image.Point{}, draw.Over)
	}
	var out bytes.Buffer
	encoder := png.Encoder{CompressionLevel: png.BestSpeed}
	if err := encoder.Encode(&out, normalized); err != nil {
		return nil, err
	}
	callLog.Add("已调整 Codex 图片分辨率", map[string]any{
		"source_width": sw, "source_height": sh, "width": w, "height": h, "strategy": strategy,
	})
	return out.Bytes(), nil
}
