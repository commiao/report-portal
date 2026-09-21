#!/usr/bin/env bash
# report-portal 发布：线上跑的源码严格等于主干上的某个 commit，且可机器验证。
#
# ## 为什么不是 redeploy.sh 那样 `cat 工作树文件 | ssh`
# 那样发布物和仓库里的任何一个 commit 都对不上：本机随便一个未提交的改动都会被发
# 上去，而漂移检测只会一直报「对不上」。这里用 `git archive <commit>`——发布物按
# 定义等于那个 commit，落地后再逐文件比 sha256 复核（准则 1 / 26）。
#
# ## 只发主干（准则 18）
# 推到 origin 只保证别人**能**找到它，不保证别人**会**拿到它。从分支直发，线上那
# 份就只存在于那条分支上，下一个人从 main 出发做的任何事都会把它悄悄抹掉，且没有
# 冲突提示。闸用 `merge-base --is-ancestor` 而不是「等于 origin/main」——回滚到更
# 早的 commit 是正当操作，只要它在主干这条线上。
#
# ## 回滚语义（准则 7）
# report-portal 无状态、可换容器：回滚 = 把 .env 里的镜像标签指回上一个 sha 再 up。
# 不需要 kg-hub 那套排空窗口（它有在飞的抽取，这个没有）。
#
# 用法：
#   deploy/release.sh                    # 发布 origin/main
#   deploy/release.sh --sha <commit>     # 发布指定 commit（必须在主干上）
#   deploy/release.sh --dry-run          # 只跑闸门与计划，不动 NAS
#   deploy/release.sh rollback <sha>     # 回滚到盘上已有的某个镜像标签
set -euo pipefail

NAS="${PORTAL_NAS_SSH:-commiao@100.123.208.32}"
SRC="${PORTAL_NAS_SRC:-/volume1/docker/report-portal-src}"
DK="${PORTAL_DOCKER:-sudo -n /var/packages/ContainerManager/target/usr/bin/docker}"
PROJECT="${PORTAL_COMPOSE_PROJECT:-report-portal-src}"
SERVICE="${PORTAL_COMPOSE_SERVICE:-report_portal}"
CONTAINER="${PORTAL_CONTAINER:-report-portal}"
IMAGE="${PORTAL_IMAGE:-report-portal}"
HEALTH_URL="${PORTAL_HEALTH_URL:-http://100.123.208.32:17172/health}"
PORTAL_URL_="${PORTAL_URL:-http://100.123.208.32:17172/portal}"
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

LOCK="$SRC/.release.lock"
lock_acquired=0

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
on_nas() { ssh "${SSH_OPTS[@]}" "$NAS" "$@"; }

# ---- 备份保留：留最近 N 份，其余删 ------------------------------------------
# 备份本身是准则 5 要的，保留策略是它的配套——没有的话它无界增长，而且**没人会
# 发现它在涨**（实测：一天 20 个）。
#
# 按**份数**保留而不是按天龄：发布是突发式的（今天几小时内 20 次），按天龄要么
# 一次清光、要么什么都不清；按份数才保证「总能回退最近 N 次」。
#
# ⚠️ `$SRC/..` 是**共享目录**：同级还住着 kg-hub-src、skill-sync-gateway、
# report-portal-legacy-backups 等。所以只认我们自己造的那两个确切文件名前缀，
# 且只删普通文件（`-type f`）——绝不碰目录，也绝不用宽 glob。
prune_backups() {
  local keep="${PORTAL_BACKUP_KEEP:-10}" out rc
  # 远端脚本走 **quoted heredoc**：本地一律不展开，参数按位置传。
  # 上一版把整段塞进双引号 ssh 字符串，转义层层嵌套还报了警告——今天已经被
  # 「f-string 里套转义引号」咬过两次，同一个坑不值得再踩第三次。
  set +e
  out=$(ssh "${SSH_OPTS[@]}" "$NAS" "bash -s -- '$SRC' '$keep'" <<'REMOTE'
set -u
parent=$(dirname "$1"); keep="$2"
cd "$parent" 2>/dev/null || exit 9
files=$(ls -1t report-portal-src.backup-*.tgz report-portal-src.rollback-*.tgz 2>/dev/null)
printf 'TOTAL=%s\n' "$(printf '%s' "$files" | grep -c . || true)"
printf '%s\n' "$files" | tail -n +$((keep + 1)) | grep . | while read -r f; do
  # 再匹配一次名字并要求是普通文件：同级目录还住着别的服务和
  # report-portal-legacy-backups/，删错代价远大于少删。
  # 这里**刻意不用 case**：macOS 自带的 bash 3.2 会把 `$( )` 里 heredoc 中的
  # `;;` 误解析成语法错误（最小复现：同样的 heredoc 去掉 case 就正常）。
  [ -f "$f" ] || continue
  keepit=0
  [ "${f#report-portal-src.backup-}" != "$f" ] && keepit=1
  [ "${f#report-portal-src.rollback-}" != "$f" ] && keepit=1
  [ "${f%.tgz}" != "$f" ] || keepit=0
  [ "$keepit" = 1 ] || continue
  rm -f -- "$f" && printf 'DEL=%s\n' "$f"
done
du -ch report-portal-src.backup-*.tgz report-portal-src.rollback-*.tgz 2>/dev/null \
  | tail -1 | awk '{print "SIZE=" $1}'
REMOTE
)
  rc=$?
  set -e
  # 拿不到 TOTAL 标记 = 这次没列成，什么都不删。失败方向恒为「保留」——
  # 少删一份只是多占几十 K，删错一份是不可逆的。
  if [ "$rc" != "0" ] || ! printf '%s' "$out" | grep -q '^TOTAL='; then
    say "   备份保留：列不出备份（退出 $rc），本次不清理"
    return 0
  fi
  local total deleted size
  total=$(printf '%s' "$out" | sed -n 's/^TOTAL=//p')
  deleted=$(printf '%s\n' "$out" | grep -c '^DEL=' || true)
  size=$(printf '%s' "$out" | sed -n 's/^SIZE=//p')
  say "   备份保留：原有 $total 份，删 $deleted 份（保留最近 $keep），当前占用 ${size:-?}"
}


# ---- 发布锁：两个发布交错会互相删文件 --------------------------------------
# 这个仓有明确的多 actor 撞车史（见 DEPLOY.md 开头），而第 5.5 步现在会**删**
# 文件。两个发布交错时的形态很具体：A 刚 archive 落地、还没走完，B 的 prune 看到
# 那些文件不在自己发的那个 ref 的祖先链上，就把 A 刚放上去的删了——事后谁都说不清
# 那个文件为什么没了。kg-hub 真机上撞见过同形的窗口（发布中的文件被判成孤儿）。
# 用 mkdir 而不是文件存在性判断：mkdir 是原子的，两个并发只有一个能建成。
acquire_lock() {
  on_nas "
    if mkdir '$LOCK' 2>/dev/null; then
      printf '%s %s %s\n' \"\$(date -Iseconds)\" '$(hostname -s)' \"\$\$\" > '$LOCK/owner'
      exit 0
    fi
    # 超时锁要能抢占，否则一次崩溃就把发布路径永久堵死（比并发更糟）。
    if [ -n \"\$(find '$LOCK' -maxdepth 0 -mmin +40 2>/dev/null)\" ]; then
      rm -rf '$LOCK'; mkdir '$LOCK'
      printf '%s %s %s (抢占了超时的旧锁)\n' \"\$(date -Iseconds)\" '$(hostname -s)' \"\$\$\" > '$LOCK/owner'
      exit 0
    fi
    echo \"持有者：\$(cat '$LOCK/owner' 2>/dev/null)\" >&2
    exit 1
  " || die "取不到发布锁——另一个发布正在进行（超过 40 分钟的陈旧锁会被自动抢占）"
  lock_acquired=1
}

# 只释放**自己拿到的**那把锁：抢占失败时若照样 rm，等于把别人正在用的锁删掉。
release_lock() {
  [ "$lock_acquired" = 1 ] || return 0
  on_nas "rm -rf '$LOCK'" >/dev/null 2>&1 || true
}
trap release_lock EXIT

# ---- prune：清掉 NAS 上多余、且内容可从 git 取回的文件 ----------------------
# 发布和回滚共用这一份。抄两遍的话，判据改一次要改两处——而这套东西的全部价值
# 就在判据上（T-0099 抱怨的正是「共享逻辑抄了四遍」）。
#
# 用法：prune_extras <基准ref> [额外可取回的ref]
# 判据是 `--list-prunable`：内容能在基准 ref 的祖先链里找到。回滚时多给一个
# 「被撤销那次发布」的 ref —— 那些残留正是它放上去的，定义上可从那条线取回。
# 不是放宽成 --all：`--all` 会把任何分支都算进来，这里只认调用方明确指出的一条。
prune_extras() {
  local ref="$1" extra_ref="${2:-}"
  local argv=(--ref "$ref" --list-prunable)
  [ -n "$extra_ref" ] && argv+=(--recoverable-from "$extra_ref")

  set +e
  local extra rc
  extra=$(python3 "$REPO/deploy/check_source_drift.py" "${argv[@]}" 2>/dev/null)
  rc=$?
  set -e

  # 「没有多余文件」和「这次没查成」必须分开报；判据是**非 0**而不是「等于某个
  # 预料中的码」——检查器崩掉给的是 rc=1，输出同样为空，会直接落进「没有多余
  # 文件」，把崩溃播报成清理干净。失败方向永远是「不删」。
  if [ "$rc" != "0" ]; then
    say "      ⚠️ 拿不到清单（检测退出 $rc），本次不删任何东西"
    say "         宁可留着让漂移检测继续报，也不在没查清时删生产上的文件"
    return 0
  fi
  if [ -z "$extra" ]; then
    say "      没有多余文件"
    return 0
  fi
  local pruned=0 path
  while read -r path; do
    [ -n "$path" ] || continue
    # 路径校验：只在 $SRC 里删，绝不让 .. 或绝对路径跑出去（清单是外部命令输出）。
    case "$path" in
      /*|*..*) say "      拒绝删除可疑路径：$path"; continue ;;
    esac
    say "      删除：$path"
    on_nas "rm -f -- '$SRC/$path'"
    pruned=$((pruned + 1))
  done <<EOF
$extra
EOF
  on_nas "find '$SRC' -mindepth 1 -type d -empty -delete 2>/dev/null || true"
  say "      共清理 $pruned 个"
}


REF="origin/main"
DRY=0
MODE="release"
ROLLBACK_SHA=""

while [ $# -gt 0 ]; do
  case "$1" in
    rollback) MODE="rollback"; ROLLBACK_SHA="${2:-}"; shift 2 || shift ;;
    --sha) REF="${2:?--sha 需要一个 commit}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

# ---- 回滚：只切标签，不重建（镜像必须已在盘上）-----------------------------
if [ "$MODE" = "rollback" ]; then
  [ -n "$ROLLBACK_SHA" ] || die "rollback 需要一个镜像标签（sha）"
  acquire_lock   # 回滚也写 .env、也重启容器、现在还会删文件，和发布互斥
  on_nas "$DK image inspect $IMAGE:$ROLLBACK_SHA >/dev/null 2>&1" \
    || die "NAS 上没有镜像 $IMAGE:$ROLLBACK_SHA，无法回滚到它"

  # 先记下**被撤销的是哪一次**，再覆盖 .env。下面清理残留要靠它：那些多出来的
  # 文件正是这次发布放上去的，可从它的祖先链取回。读不到就不清，只报。
  UNDOING=$(on_nas "grep '^PORTAL_IMAGE_TAG=' $SRC/.env 2>/dev/null | cut -d= -f2-" || true)

  say "回滚到 $IMAGE:$ROLLBACK_SHA（撤销 ${UNDOING:-<读不到上一个标签>}）"
  on_nas "cd $SRC && printf 'PORTAL_IMAGE_TAG=%s\n' '$ROLLBACK_SHA' > .env && \
          $DK compose -p $PROJECT up -d --no-build $SERVICE >/dev/null 2>&1 && echo ok"
  sleep 3
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)
  say "health=$code"
  [ "$code" = "200" ] || die "回滚后健康检查未通过"

  # 服务已恢复，再让源码树跟着回去。顺序刻意如此：镜像不可变，源码内容不影响
  # 正在跑的容器，所以服务优先；源码没同步上只是「待修」，不该拖着服务不恢复。
  if ! git -C "$REPO" cat-file -e "${ROLLBACK_SHA}^{commit}" 2>/dev/null; then
    say "⚠️ 本地没有 commit $ROLLBACK_SHA，源码树未同步（服务已回滚）"
    exit 0
  fi

  # 马上要删东西了，先备份整树（准则 5：备份 ⊇ 覆盖，而「覆盖」现在含删除）。
  STAMP=$(date +%Y%m%d-%H%M%S)
  BACKUP="$SRC/../report-portal-src.rollback-$STAMP.tgz"
  say "备份 $SRC → $BACKUP"
  on_nas "tar czf '$BACKUP' -C '$SRC' ." || die "备份失败，不继续动源码树"

  if git -C "$REPO" archive --format=tar "$ROLLBACK_SHA" \
       | ssh "${SSH_OPTS[@]}" "$NAS" "tar xf - -C '$SRC'"; then
    say "源码树已同步回 $ROLLBACK_SHA"
  else
    say "⚠️ 服务已回滚，但源码树没同步回去——漂移检测会报红，需手工处理"
    exit 0
  fi

  # archive 只覆盖不删除，所以被撤销那次发布**新增**的文件还留着。清掉它们——
  # 判据不是「不在回滚目标里就删」，而是「内容能从回滚目标 or 被撤销那次发布的
  # 祖先链取回」。生产独有、或来自第三条线的东西照样不碰，只会被报出来。
  say "清理被撤销那次发布新增的文件"
  if [ -n "$UNDOING" ]; then
    prune_extras "$ROLLBACK_SHA" "$UNDOING"
  else
    say "      读不到被撤销的标签，本次不清理（只按回滚目标报）"
    prune_extras "$ROLLBACK_SHA"
  fi
  prune_backups
  exit 0
fi

# ---- 1. 定 commit + 只发主干闸（准则 18）-----------------------------------
say "[1/7] 解析并校验 commit"
git -C "$REPO" fetch origin --quiet
SHA=$(git -C "$REPO" rev-parse "$REF^{commit}") || die "解析不了 $REF"
SHORT=$(git -C "$REPO" rev-parse --short "$SHA")
git -C "$REPO" merge-base --is-ancestor "$SHA" origin/main \
  || die "$SHORT 不在 origin/main 这条线上——改代码走分支，发布前先合回主干（准则 18）"
say "      发布 $SHORT（已确认在主干上）"

# 工作树脏不影响发布物（git archive 取的是 commit），但要让人知道发的不是眼前这份
if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
  say "      注意：工作树有未提交改动，它们【不会】被发布（发的是 $SHORT）"
fi

if [ "$DRY" = "1" ]; then
  say "[dry-run] 将发布 $SHORT，共 $(git -C "$REPO" archive "$SHA" | tar -t | wc -l | tr -d ' ') 个条目"
  exit 0
fi

# ---- 2. 取发布锁 + 记下回滚点（准则 6/7）-----------------------------------
acquire_lock
PREV=$(on_nas "grep '^PORTAL_IMAGE_TAG=' $SRC/.env 2>/dev/null | cut -d= -f2-" || true)
say "[2/7] 回滚点：${PREV:-<无，首次按准则发布>}"

# ---- 3. 备份整棵源目录（准则 5：备份 ⊇ 覆盖）-------------------------------
# git archive 会覆盖整棵树，所以备份也必须是整棵树——只备份「构建输入」那几个文件
# 的话，第一次真跑就会把生产独有的文件盖掉。
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$SRC/../report-portal-src.backup-$STAMP.tgz"
say "[3/7] 备份 $SRC → $BACKUP"
on_nas "tar czf '$BACKUP' -C '$SRC' . && ls -la '$BACKUP' | awk '{print \"      \" \$5 \" bytes\"}'"

# ---- 4. git archive → NAS（发布物严格等于该 commit，准则 1）-----------------
say "[4/7] git archive $SHORT → $SRC"
git -C "$REPO" archive --format=tar "$SHA" \
  | ssh "${SSH_OPTS[@]}" "$NAS" "mkdir -p '$SRC' && tar xf - -C '$SRC' && echo '      落地完成'"

# ---- 5. 落地逐文件复核（准则 26：不是「我传上去了」，是可机器验证的等于）----
say "[5/7] 逐文件复核落地内容"
# 远端哈希一次 ssh 取完（逐文件一次往返的话，十几个文件就是十几秒，而且中途断线
# 会被读成「不一致」）。比对在本地做。
NAS_HASHES=$(on_nas "cd '$SRC' && find . -type f -print0 | xargs -0 sha256sum 2>/dev/null")
mismatch=0
checked=0
while read -r path; do
  [ -n "$path" ] || continue
  want=$(git -C "$REPO" show "$SHA:$path" | shasum -a 256 | cut -d' ' -f1)
  got=$(printf '%s\n' "$NAS_HASHES" | awk -v p="./$path" '$2==p {print $1; exit}')
  checked=$((checked + 1))
  if [ "$want" != "$got" ]; then
    say "      不一致：$path"
    mismatch=$((mismatch + 1))
  fi
done < <(git -C "$REPO" ls-tree -r --name-only "$SHA")
[ "$mismatch" = "0" ] || die "$mismatch 个文件落地后与 $SHORT 不一致"
say "      $checked 个文件全部一致"

# ---- 5.5 清掉 git 里已经没有的文件 -----------------------------------------
# `git archive | tar xf` 只覆盖和新增，从不删除。不清的话，git 里删掉的文件会一直
# 留在 NAS 上——而且可能被执行；于是「线上等于某个 commit」只在「有什么」这一半
# 成立。
#
# 判据是 **--list-prunable：内容能在所发 ref 的祖先链里找到一模一样的那些**。
# 它排除了两类看起来像垃圾、其实不是的东西：
#   1.「git 里现在没有」的补集 —— 里面有生产独有但正在用的文件。kg-hub 实测过
#      deploy/hot_config_reconciliation.py：360 行、NAS 上在跑、git 里连文件名都
#      没有，误删它就是把生产打掉（T-0084 立的约束）。
#   2.「--all 里找得到就行」—— 别的分支或更新提交的内容也会算成可删，而那恰恰是
#      **部署不完整的信号**，删掉等于把信号抹了（kg-hub-edit 在真机上撞出来的）。
# 这两类都不删，但仍会被漂移检测报出来，交给人判断。
# 删除已被第 3 步的整树备份覆盖（准则 5：备份 ⊇ 覆盖，现在「覆盖」含删除）。
say "[5.5] 清理 git 已删除的文件（只删内容可在本次 ref 祖先链中找到的）"
prune_extras "$SHA"

# ---- 6. 构建不可变镜像 + 切标签起容器 --------------------------------------
say "[6/7] 构建 $IMAGE:$SHORT 并启动（不动 latest）"
on_nas "cd '$SRC' && printf 'PORTAL_IMAGE_TAG=%s\n' '$SHORT' > .env && \
        $DK compose -p $PROJECT build $SERVICE >/dev/null 2>&1 && \
        $DK compose -p $PROJECT up -d $SERVICE >/dev/null 2>&1 && echo '      已启动'"

# ---- 7. 验收：镜像来源比对 + 健康检查；不合格自动回到上一个标签 -------------
say "[7/7] 验收"
running=$(on_nas "$DK inspect '$CONTAINER' --format '{{.Config.Image}}' 2>/dev/null" || true)
say "      容器镜像：$running"

code=""
body=""
for _ in 1 2 3 4 5; do
  body=$(curl -s -m 8 -w '\n%{http_code}' "$HEALTH_URL" || true)
  code=$(printf '%s' "$body" | tail -1)
  body=$(printf '%s' "$body" | sed '$d')
  [ "$code" = "200" ] && break
  sleep 3
done
pcode=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$PORTAL_URL_" || true)
say "      health=$code  portal=$pcode"
# health 现在量的是「页面还做不做得出来」，所以把它的实质内容也打出来：
# 只看 200 的话，degraded（某个源不可达）会悄悄上线，而那正是要让人看见的。
if [ -n "$body" ]; then
  # 用 % 格式化而不是 f-string：整段 python 是裹在 shell 单引号里传进去的，
  # f-string 里再出现转义引号会当场语法错，而外面一个 `|| echo` 会把它吞成
  # 一句「解析不了」——实测就这么静默瞎了一次。
  say "      $(printf '%s' "$body" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("health 返回的不是 JSON")
    raise SystemExit
failed = ",".join(d.get("sources", {}).get("failed") or []) or "无"
print("health.status=%s 卡片=%s 不可达源=%s" % (d.get("status"), d.get("cards"), failed))' 2>&1)"
fi

if [ "$running" != "$IMAGE:$SHORT" ] || [ "$code" != "200" ] || [ "$pcode" != "200" ]; then
  if [ -n "$PREV" ] && [ "$PREV" != "$SHORT" ]; then
    say "      验收未通过 → 自动回滚到 $PREV"
    on_nas "cd '$SRC' && printf 'PORTAL_IMAGE_TAG=%s\n' '$PREV' > .env && \
            $DK compose -p $PROJECT up -d --no-build $SERVICE >/dev/null 2>&1 && echo '      已回滚'"
  fi
  die "验收未通过（镜像=$running health=$code portal=$pcode）"
fi

say "✅ $SHORT 已上线：$PORTAL_URL_"
say "   回滚：deploy/release.sh rollback ${PREV:-<上一个 sha>}"

# ---- 8. 刷新漂移判决（准则 10：能改变判决的动作，自己负责刷新它）-----------
# 不刷的话，刚发完这一刻巡检仍在报发布前那条红——而它报的那些文件正是刚被这次
# 发布对齐掉的。靠年龄阈值救不了：文件是几分钟前写的，看起来就是新鲜的。
STATUS_FILE="${PORTAL_DRIFT_STATUS:-$HOME/.cache/report-portal/source-drift.status}"
CHECKER="$REPO/deploy/check_source_drift.py"
if [ -f "$CHECKER" ]; then
  set +e
  python3 "$CHECKER" --status-file "$STATUS_FILE" >/dev/null 2>&1
  DRIFT_RC=$?
  set -e
  # 同样按返回码分开说。原来写成「非 0 一律报『已刷新：仍有待处理项』」，
  # 把「真有漂移」和「刷新压根没成功」混成一句——而准则 10 的要害就是这个动作
  # 要对判决负责，刷失败了还说「已刷新」是假话。
  case "$DRIFT_RC" in
    0) say "   漂移判决已刷新：一致" ;;
    1) say "   漂移判决已刷新：有漂移（或刷新中途失败）——以 $STATUS_FILE 的时间戳为准" ;;
    *) say "   ⚠️ 漂移判决没刷成（退出 $DRIFT_RC）：$STATUS_FILE 里可能还是发布前那条，别拿它当结论" ;;
  esac
fi
prune_backups
