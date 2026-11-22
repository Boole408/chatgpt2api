package repository

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func TestTrimLogsKeepsNewestWithSameTimestamp(t *testing.T) {
	repo, err := Open("sqlite:///" + filepath.Join(t.TempDir(), "logs.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()
	ctx := context.Background()
	now := time.Now().UTC()
	for _, item := range []Log{
		{ID: "d", Time: now.Add(-time.Second)},
		{ID: "a", Time: now},
		{ID: "c", Time: now},
		{ID: "b", Time: now},
	} {
		item.Detail = map[string]any{}
		if err := repo.SaveLog(ctx, item); err != nil {
			t.Fatal(err)
		}
	}
	for _, keep := range []int{5, 2, 2} {
		if err := repo.TrimLogs(ctx, keep); err != nil {
			t.Fatal(err)
		}
		items, total, err := repo.Logs(ctx, "", "", 1, 10)
		if err != nil {
			t.Fatal(err)
		}
		want := min(keep, 4)
		if total != int64(want) || len(items) != want || items[0].ID != "c" || items[1].ID != "b" {
			t.Fatalf("keep %d: total %d, items %+v", keep, total, items)
		}
	}
}
