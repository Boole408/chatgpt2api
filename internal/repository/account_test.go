package repository

import (
	"context"
	"path/filepath"
	"slices"
	"sync"
	"testing"
)

func TestAccountPageSortsBeforePagination(t *testing.T) {
	repo, err := Open("sqlite:///" + filepath.Join(t.TempDir(), "accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()
	ctx := context.Background()
	var accounts []Account
	for i, remaining := range []int{0, 2, 7, 12, 7} {
		email := string(rune('a'+i)) + "@example.test"
		accounts = append(accounts, Account{Email: &email, AccessToken: email, Remaining: remaining})
	}
	if _, _, err := repo.SaveAccounts(ctx, accounts); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		page, size int
		direction  string
		want       []int64
	}{
		{1, 2, "", []int64{4, 3}},
		{2, 2, "", []int64{5, 2}},
		{1, 2, "asc", []int64{1, 2}},
	} {
		items, total, _, err := repo.AccountPage(ctx, tc.page, tc.size, "", "", "", "remaining", tc.direction)
		if err != nil || total != 5 {
			t.Fatalf("list accounts: total = %d, err = %v", total, err)
		}
		var ids []int64
		for _, item := range items {
			ids = append(ids, item.ID)
		}
		if !slices.Equal(ids, tc.want) {
			t.Fatalf("page %d (%q): ids = %v, want %v", tc.page, tc.direction, ids, tc.want)
		}
	}
}

func TestAcquireImageAccountSpreadsConcurrentCalls(t *testing.T) {
	repo, err := Open("sqlite:///" + filepath.Join(t.TempDir(), "accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()
	ctx := context.Background()
	var accounts []Account
	for i, item := range []Account{
		{Type: "plus", IsCodex: true, Quota: 1},
		{Type: "plus", IsCodex: true, Quota: 10},
		{Type: "plus", IsCodex: true, Quota: 20},
		{Type: "plus", IsCodex: true, Quota: 0},
		{Type: "plus", IsCodex: true, Quota: 20, Status: "禁用"},
		{Type: "free", IsCodex: true, Quota: 20},
		{Type: "plus", Quota: 20},
	} {
		email := string(rune('a'+i)) + "@example.test"
		item.Email, item.AccessToken = &email, email
		accounts = append(accounts, item)
	}
	if _, _, err := repo.SaveAccounts(ctx, accounts); err != nil {
		t.Fatal(err)
	}
	routes, plans := []string{"codex"}, []string{"plus"}
	claimed := make(chan int64, 3)
	var wg sync.WaitGroup
	for range 3 {
		wg.Go(func() {
			item, acquired, err := repo.AcquireImageAccount(ctx, routes, plans, 3, nil)
			if err != nil || !acquired {
				t.Errorf("acquire: acquired = %t, err = %v", acquired, err)
				return
			}
			claimed <- item.ID
		})
	}
	wg.Wait()
	close(claimed)
	var ids []int64
	for id := range claimed {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	if !slices.Equal(ids, []int64{1, 2, 3}) {
		t.Fatalf("concurrent calls did not spread across eligible accounts: %v", ids)
	}
	attempted := map[string]bool{*accounts[1].Email: true, *accounts[2].Email: true}
	if _, acquired, err := repo.AcquireImageAccount(ctx, routes, plans, 3, attempted); err != nil || acquired {
		t.Fatalf("one remaining credit already reserved: acquired = %t, err = %v", acquired, err)
	}
	if err := repo.ReleaseImageAccountByID(ctx, 1); err != nil {
		t.Fatal(err)
	}
	if item, acquired, err := repo.AcquireImageAccount(ctx, routes, plans, 3, attempted); err != nil || !acquired || item.ID != 1 {
		t.Fatalf("released slot not reusable: id = %d, acquired = %t, err = %v", item.ID, acquired, err)
	}
}

func TestImageAccountAvailable(t *testing.T) {
	repo, err := Open("sqlite:///" + filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer repo.Close()
	ctx := context.Background()
	email := "a@example.com"
	account := Account{AccessToken: "token", Email: &email, AccountID: "acc", Type: "free", Status: "正常", Quota: 5, CreatedAt: "2026-11-22T00:00:00Z"}
	if _, _, err := repo.SaveAccounts(ctx, []Account{account}); err != nil {
		t.Fatal(err)
	}

	for _, test := range []struct {
		name      string
		routes    []string
		plans     []string
		attempted map[string]bool
		want      bool
	}{
		{"available", []string{"web"}, nil, nil, true},
		{"attempted", []string{"web"}, nil, map[string]bool{email: true}, false},
		{"codex route needs codex account", []string{"codex"}, nil, nil, false},
		{"plan mismatch", []string{"web"}, []string{"plus"}, nil, false},
	} {
		available, err := repo.ImageAccountAvailable(ctx, test.routes, test.plans, test.attempted)
		if err != nil {
			t.Fatalf("%s: %v", test.name, err)
		}
		if available != test.want {
			t.Errorf("%s: available = %v, want %v", test.name, available, test.want)
		}
	}

	if _, err := repo.UpdateAccount(ctx, email, map[string]any{"quota": 0}); err != nil {
		t.Fatal(err)
	}
	if available, err := repo.ImageAccountAvailable(ctx, []string{"web"}, nil, nil); err != nil || available {
		t.Fatalf("quota exhausted: available = %v, err = %v", available, err)
	}
}
