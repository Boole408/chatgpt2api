package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/basketikun/chatgpt2api/internal/config"
	"github.com/basketikun/chatgpt2api/internal/repository"
	"github.com/basketikun/chatgpt2api/internal/service"
)

func migrateLegacy(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("migrate-legacy", flag.ContinueOnError)
	accounts := flags.String("accounts", "", "Python 版 accounts.json 的路径")
	keys := flags.String("keys", "", "Python 版 auth_keys.json 的路径")
	apply := flags.Bool("apply", false, "写入 Go 数据库（默认只检查源文件）")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || (*accounts == "" && *keys == "") {
		return fmt.Errorf("需要指定 -accounts 或 -keys；加 -apply 才会写入")
	}
	snapshot, err := service.ReadLegacySnapshot(*accounts, *keys)
	if err != nil {
		return err
	}
	if *apply {
		cfg, err := config.Load("config.yaml")
		if err != nil {
			return err
		}
		repo, err := repository.Open(cfg.DatabaseURL)
		if err != nil {
			return err
		}
		defer repo.Close()
		if err := repo.InsertLegacySnapshot(ctx, snapshot.Accounts, snapshot.Keys); err != nil {
			return err
		}
	}
	fmt.Printf("账号：%d；用户密钥：%d；跳过的旧管理员密钥：%d；已写入：%t\n",
		len(snapshot.Accounts), len(snapshot.Keys), snapshot.SkippedAdmin, *apply)
	return nil
}
