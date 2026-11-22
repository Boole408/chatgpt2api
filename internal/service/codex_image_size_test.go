package service

import (
	"bytes"
	"context"
	"errors"
	"image"
	"image/color"
	"image/png"
	"testing"
)

func TestCodexImageSizePreservesOutputContract(t *testing.T) {
	src := image.NewNRGBA(image.Rect(0, 0, 40, 40))
	for y := range 40 {
		for x := range 40 {
			c := color.NRGBA{G: 255, A: 255}
			if y < 5 {
				c = color.NRGBA{R: 255, A: 255}
			} else if y >= 35 {
				c = color.NRGBA{B: 255, A: 255}
			}
			src.SetNRGBA(x, y, c)
		}
	}
	var raw bytes.Buffer
	if err := png.Encode(&raw, src); err != nil {
		t.Fatal(err)
	}
	for _, size := range []string{"auto", "40x40"} {
		out, err := normalizeCodexImage(context.Background(), raw.Bytes(), size, nil)
		if err != nil || !bytes.Equal(out, raw.Bytes()) {
			t.Fatalf("normalization unexpectedly changed %s output: %v", size, err)
		}
	}
	for _, tt := range []struct {
		size string
		w, h int
	}{
		{"2048x2048", 2048, 2048},
		{"120x40", 120, 40},
	} {
		out, err := normalizeCodexImage(context.Background(), raw.Bytes(), tt.size, nil)
		if err != nil {
			t.Fatal(err)
		}
		img, err := png.Decode(bytes.NewReader(out))
		if err != nil {
			t.Fatal(err)
		}
		if img.Bounds().Dx() != tt.w || img.Bounds().Dy() != tt.h {
			t.Fatalf("%s resized to %v", tt.size, img.Bounds())
		}
		rTop, _, _, _ := img.At(tt.w/2, tt.h/32).RGBA()
		_, _, bBottom, _ := img.At(tt.w/2, tt.h-1-tt.h/32).RGBA()
		if rTop < 0xe000 || bBottom < 0xe000 {
			t.Fatalf("%s clipped source edge markers", tt.size)
		}
	}
	if _, err := normalizeCodexImage(context.Background(), raw.Bytes(), "8192x8192", nil); !errors.Is(err, errInvalidCodexSize) {
		t.Fatalf("expected unsafe size rejection, got %v", err)
	}
}
