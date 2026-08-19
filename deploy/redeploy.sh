#!/usr/bin/env bash
# 一键把 report-portal 部署到 NAS（compose 项目 report-portal-src）并探活。
#
# 与 kg-hub 解耦：这是它自己的容器/项目/端口(17172)，只借用 kg-hub 的 docker 网络
# (kg-hub_default，external) 做服务端 manifest 抓取。改完代码跑这一条即可。
#
# ⚠️ 单一项目名约定：compose 项目名固定 report-portal-src（= NAS 源码目录名，也是
# 线上正在跑的项目名）。多 actor 若用不同项目名会各建容器抢 :17172 → 端口冲突/漂移。
# 任何管理 report-portal 的一方都必须用这个项目名，别再用 -p report-portal。
#
# 用法：
#   deploy/redeploy.sh            # 同步全部源码 + 重建重启 + 探活
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="/volume1/docker/report-portal-src"
DK="sudo -n /var/packages/ContainerManager/target/usr/bin/docker"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FILES="portal.py Dockerfile docker-compose.yml requirements.txt .dockerignore"

echo "[1/3] 同步源码到 NAS（原子 tmp+mv）"
ssh -o BatchMode=yes "$NAS" "mkdir -p \"$SRC\""
for f in $FILES; do
  printf '      %s … ' "$f"
  cat "$REPO/$f" | ssh -o BatchMode=yes "$NAS" \
    "cat > \"$SRC/.dep.tmp\" && mv -f \"$SRC/.dep.tmp\" \"$SRC/$f\" && echo ok"
done

echo "[2/3] 重建镜像 + 重启容器（project=report-portal-src）"
ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "cd $SRC && $DK compose -p report-portal-src build report_portal >/dev/null 2>&1 && \
   $DK compose -p report-portal-src up -d report_portal >/dev/null 2>&1 && echo '      done'"

echo "[3/3] 探活"
URL="${PORTAL_URL:-http://100.123.208.32:17172}"
sleep 3
for i in 1 2 3 4 5; do
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$URL/health" || true)
  [ "$code" = "200" ] && break; sleep 3
done
portal=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$URL/portal" || true)
echo "      health=$code  portal=$portal"
echo "→ 打开 $URL/portal"
