#!/usr/bin/env bash
# 在服务器上更新已部署的平台：拉代码 → 装依赖 → 收集静态文件 → 迁移 → 重启
#
# 用法（任意目录均可）：
#   ~/Desktop/DIP-Competition/deploy/update.sh
#
# 前提：项目目录由 git clone 得到，.env / .venv / data / storage 为本地忽略项。
set -euo pipefail

cd "$(dirname "$0")/.."
echo "== 项目目录：$(pwd)"

before="$(git rev-parse HEAD)"
echo "== git pull"
git pull --ff-only
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "== 已是最新提交 $after，仍继续执行后续步骤"
fi

echo "== 依赖"
if git diff --name-only "$before" "$after" -- requirements.txt | grep -q .; then
    echo "requirements.txt 有变化，安装依赖"
    .venv/bin/python -m pip install -q -r requirements.txt
else
    echo "requirements.txt 未变化，跳过"
fi

echo "== 静态文件"
.venv/bin/python manage.py collectstatic --noinput | tail -1

echo "== 数据库迁移"
.venv/bin/python manage.py migrate --noinput | tail -2

echo "== 重启服务"
systemctl --user restart dip-competition
sleep 2
systemctl --user is-active dip-competition

echo "== 完成：$(git log --oneline -1)"
