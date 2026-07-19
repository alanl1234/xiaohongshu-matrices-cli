#!/bin/bash
# 从上游 jackwener/xiaohongshu-cli 拉最新代码并合并
# 首次使用: git remote add upstream https://github.com/jackwener/xiaohongshu-cli.git

set -euo pipefail

UPSTREAM="${1:-upstream}"
BRANCH="${2:-main}"

echo ">>> 拉取上游 $UPSTREAM/$BRANCH ..."
git fetch "$UPSTREAM" "$BRANCH"

echo ""
echo ">>> 本地相对于上游的差异:"
git diff "$UPSTREAM/$BRANCH" --stat

echo ""
echo ">>> 执行合并: git merge $UPSTREAM/$BRANCH"
git merge "$UPSTREAM/$BRANCH" --no-edit

echo ""
echo ">>> 合并完成。如有冲突请手动解决后: git add . && git merge --continue"
