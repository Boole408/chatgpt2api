package repository

import (
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
)

func TestOpenRejectsPythonSQLiteWithoutMigratingItsSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE accounts (id INTEGER PRIMARY KEY, access_token TEXT, data TEXT)"); err != nil {
		t.Fatal(err)
	}
	repo, err := Open("sqlite:///" + path)
	if repo != nil {
		defer repo.Close()
	}
	if !errors.Is(err, errLegacyDatabase) {
		t.Fatalf("legacy database accepted: %v", err)
	}
	var mode string
	if err := db.QueryRow("PRAGMA journal_mode").Scan(&mode); err != nil || mode != "delete" {
		t.Fatalf("legacy journal mode = %q, error = %v", mode, err)
	}
	var tables int
	if err := db.QueryRow(`SELECT count(*) FROM sqlite_master WHERE type = 'table'`).Scan(&tables); err != nil || tables != 1 {
		t.Fatalf("legacy table count = %d, error = %v", tables, err)
	}
}
