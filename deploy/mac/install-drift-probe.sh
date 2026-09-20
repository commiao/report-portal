#!/usr/bin/env bash
# 安装 report-portal 源码漂移巡检（Mac 侧 launchd 日更作业）。
#
# 它做两件事：
#   1. 备好私有 clone  ~/.local/share/report-portal/repo
#      —— 探针跑的是这份，不是共享开发工作树（准则 20）。别人在工作树里的在途
#         改动不会悄悄改变探针行为，探针也不会因为谁在编辑而读到半截文件。
#   2. 渲染并装载 launchd 作业，判决写进
#      ~/.cache/report-portal/source-drift.status（fleet-ops SessionStart 读它）
#
# 幂等：重复跑只会 fetch + 重装。
set -euo pipefail

LABEL="com.report-portal.source-drift"
CLONE="$HOME/.local/share/report-portal/repo"
STATUS="$HOME/.cache/report-portal/source-drift.status"
PLIST_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agents/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
REMOTE="${PORTAL_GIT_REMOTE:-git@github-commiao:commiao/report-portal.git}"

say() { printf '%s\n' "$*"; }

say "[1/3] 私有 clone：$CLONE"
if [ -d "$CLONE/.git" ]; then
  git -C "$CLONE" fetch origin --quiet
  say "      已存在，fetch 完成"
else
  mkdir -p "$(dirname "$CLONE")"
  git clone --quiet "$REMOTE" "$CLONE"
  say "      已克隆"
fi
# 探针只读 origin/main，不需要检出主干；反而**不检出 main** 更好（准则 18 的配套
# 做法）：没有检出就不可能有人在这棵树上提交。
git -C "$CLONE" checkout --quiet --detach origin/main

say "[2/3] 渲染并装载 $LABEL"
mkdir -p "$(dirname "$PLIST_DST")" "$(dirname "$STATUS")" "$HOME/Library/Logs"
sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
# 用 Label 卸载，不要用文件名推（准则 27：按文件名 bootout 一个不存在的 job 会
# 静默成功，旧定义一直没卸掉）。
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
say "      已装载（RunAtLoad，稍后会写第一份判决）"

say "[3/3] 立刻跑一次，确认判决写得出来"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$STATUS" ] && break
  sleep 2
done
if [ -s "$STATUS" ]; then
  say "      $STATUS："
  say "      $(cat "$STATUS")"
else
  say "      ⚠️ 还没写出判决——看 ~/Library/Logs/report-portal-source-drift.err.log"
  exit 1
fi

say
say "接入 SessionStart：fleet-ops/bin/ops-hook-context.sh 的 DRIFT_SERVICES 需有一行"
say "  report-portal|\$HOME/.cache/report-portal/source-drift.status|report-portal T-0090；细节跑 report-portal/deploy/check_source_drift.py"
