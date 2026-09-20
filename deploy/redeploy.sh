#!/usr/bin/env bash
# 已停用——请改用 deploy/release.sh。
#
# 这个脚本原来做的是 `cat 工作树文件 | ssh`：把本机工作树里当下那一份传上 NAS 再
# 重建。问题不是它不好用，而是它让「线上跑的是哪个 commit」这个问题没有答案——
# 任何未提交的改动都会被发上去，漂移检测只能一直报「对不上」。
#
# 更要命的是准则 21：绕过发布路径改一次生产，正式发布路径本身也会失效。只要这条
# 旁路还在，release.sh 的「只发主干」闸、备份、落地复核、自动回滚就全都可以被绕过，
# 那它们就等于不存在。所以这里直接堵死，而不是留着「紧急时还能用」。
#
# 对应关系：
#   旧 redeploy.sh                     新 deploy/release.sh
#   传工作树                            git archive <commit>（发布物 = 某个 commit）
#   无闸                                merge-base --is-ancestor <sha> origin/main
#   无备份                              发布前整树 tgz 备份
#   传完就算                            落地后逐文件 sha256 复核
#   无回滚                              验收失败自动回上一个标签；rollback 子命令
set -euo pipefail

cat >&2 <<'EOF'
deploy/redeploy.sh 已停用（它从开发工作树直发，线上版本无法追溯到 commit）。

请改用：
  deploy/release.sh              # 发布 origin/main
  deploy/release.sh --dry-run    # 只跑闸门，不动 NAS
  deploy/release.sh rollback <sha>

发布前请先 git commit && git push——release.sh 只发主干上的 commit。
EOF
exit 2
