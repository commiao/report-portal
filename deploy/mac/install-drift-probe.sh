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

# launchd 重载的公共实现由 fleet-ops 产物提供（准则 32：机制不能依赖被安装方
# 自己提供）。**库不在就直接失败**，不回退到手写 bootout+bootstrap。
LAUNCHD_LIB="${FLEET_OPS_LAUNCHD_LIB:-$HOME/.local/share/fleet-ops/current/platform/darwin/launchd.sh}"
if [ ! -f "$LAUNCHD_LIB" ]; then
  echo "缺少 fleet-ops 的 launchd 库：$LAUNCHD_LIB" >&2
  echo "  先装 fleet-ops： sh ~/workspace_claudeCode/fleet-ops/platform/darwin/install.sh" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "$LAUNCHD_LIB"

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
# 重载走 fleet-ops 的公共实现。这里原本已经做对了一半 —— 用 Label 不用文件名
# （准则 27）—— 但漏了另一半：**`bootout` 返回不等于作业已退干净**，紧跟着
# bootstrap 会失败且不重试，结果是旧的已卸、新的没装（准则 29 的推论，
# fleet-ops T-0142：批量切 4 个作业时四个全灭，停了约两分钟）。
# 记住一条准则不等于记住相邻的那条，所以两半都收进 launchd_reload：
# 它自己从 plist 内容读 Label，也自己等旧作业退干净。
launchd_reload "$PLIST_DST"
say "      已装载（RunAtLoad，稍后会写第一份判决）"

say "[3/3] 立刻跑一次，确认判决写得出来"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$STATUS" ] && break
  sleep 2
done
if [ -s "$STATUS" ]; then
  say "      ${STATUS}："
  say "      $(cat "$STATUS")"
else
  say "      ⚠️ 还没写出判决——看 ~/Library/Logs/report-portal-source-drift.err.log"
  exit 1
fi

say
say "接入 SessionStart：fleet-ops/bin/ops-hook-context.sh 的 DRIFT_SERVICES 需有一行"
say "  report-portal|\$HOME/.cache/report-portal/source-drift.status|report-portal T-0090；细节跑 report-portal/deploy/check_source_drift.py"
