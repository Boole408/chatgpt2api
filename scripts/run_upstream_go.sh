#!/bin/sh
set -eu

cd "$(dirname "$0")/../upstream-go"

# Keep the Go preview independent of the Python process and its environment.
: "${CHATGPT2API_GO_AUTH_KEY:?请先设置 CHATGPT2API_GO_AUTH_KEY 作为 Go 版管理员密钥}"
export CHATGPT2API_AUTH_KEY="$CHATGPT2API_GO_AUTH_KEY"
export DATABASE_URL="sqlite:///data/chatgpt2api.db"
export BIND_ADDR="127.0.0.1"
export PORT="${CHATGPT2API_GO_PORT:-3002}"

exec go run .
