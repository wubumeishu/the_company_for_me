#!/usr/bin/env bash
# 安装第二道防线 hooks（软链到 .git/hooks，随 checkout 失效可重跑）
set -e
cd "$(dirname "$0")/.."
for h in pre-push commit-msg; do
  chmod +x "scripts/git-hooks/$h"
  ln -sf "$(pwd)/scripts/git-hooks/$h" ".git/hooks/$h"
  echo "✓ .git/hooks/$h -> scripts/git-hooks/$h"
done
echo "第二道防线安装完成（引擎权威层仍在 git_policy.py；CI 第三道见 .github/workflows/ci.yml）"
