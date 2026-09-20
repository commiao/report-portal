#!/usr/bin/env python3
"""report-portal 源码漂移检测：NAS 上会被执行的东西，是不是等于主干某个 commit。

为什么两侧都要列（准则 4）
--------------------------
只按 git 清单去查，永远看不见「只在生产上存在」的文件——kg-hub 就踩过：
`deploy/hot_config_reconciliation.py` 360 行在生产跑着，git 里连文件名都没有，
而当时的检测显示「✅ 对得上」。所以 git 侧用 `ls-tree`，远端用 `find`，**取并集**。

为什么排除项用黑名单（准则 12）
------------------------------
按「漏掉的后果」选，不按形式选：扫描时漏排一个只会**多报**一个文件让人看一眼；
而取样打包时漏排会把机密打进包里。这里是扫描，所以黑名单是安全的一侧。

用法：
    deploy/check_source_drift.py                 # 比 origin/main
    deploy/check_source_drift.py --ref <commit>
    deploy/check_source_drift.py --json
    deploy/check_source_drift.py --status-file ~/.cache/report-portal/source-drift.status
退出码：0 = 一致；1 = 有漂移；2 = 取数失败（不要把取数失败当成「一致」）。

`--status-file` 写的是 fleet-ops SessionStart 巡检读的那份契约：
`<ISO8601>\t<ok|drift|error>\t<一行详情>`。三件事按契约必须分开：
「一致」「漂了」「这次没查成」——把第三种写成 ok，等于 ssh 一挂就播报体检通过。
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

NAS = "commiao@100.123.208.32"
SRC = "/volume1/docker/report-portal-src"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]

# 生产侧本就该有、且不属于「发布物」的东西。漏排只会多报一条，安全。
IGNORE_EXACT = {".env"}
IGNORE_PREFIX = (".git/", "__pycache__/", ".release")
IGNORE_SUFFIX = (".pyc", ".tgz", ".backup", ".dep.tmp", ".reconcile.tmp")


def ignored(path: str) -> bool:
    if path in IGNORE_EXACT or path.startswith(IGNORE_PREFIX):
        return True
    if any(seg == "__pycache__" for seg in path.split("/")):
        return True
    return path.endswith(IGNORE_SUFFIX)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def was_ever_tracked(path: str) -> bool:
    """这条路径在 git 历史里出现过吗？（只用来把跳过的原因说清楚）"""
    out = subprocess.run(["git", "log", "--all", "--oneline", "-1", "--", path],
                         capture_output=True, text=True)
    return out.returncode == 0 and bool(out.stdout.strip())


def historical_hashes(path: str, ref: str = None) -> set:
    """这条路径在 **ref 这条祖先链**上出现过的全部内容指纹（sha256）。
    `ref=None` 表示放宽到 `--all`（任何分支、任何提交），只用来分类、不用来放行。

    范围必须是祖先链，不能是 `--all`（kg-hub-edit 会话在真机上撞出来的）：
    NAS 跑着 49bc2d87，目录里却躺着来自**更新的** 9ef37b5 的文件——用 `--all` 查
    「历史里找得到一模一样的内容」，于是判可删。可它不是孤儿，**是部署不完整的
    信号**；删掉等于把信号抹了，下次照样发生而没人知道为什么。更一般地：`--all`
    会把**任何分支**上的内容都算成可删，于是别人从分支拷一份到生产，发布就替他
    删掉了。

    删除的判据是「NAS 上这一份，能在 git 历史里找到一模一样的内容」——比
    「这条路径曾被跟踪」更紧一档，而且紧的那一档正是要害：**被跟踪过 ≠ NAS 上
    那份还等于历史里某一版**。生产上被手改过、git 后来又删掉的文件，只满足前者；
    删了它，那些改动就真没了（report-portal 这边有发布前整树备份兜底，但那是
    静默的——没人会知道去翻备份，等于悄悄毁掉一份唯一的东西）。

    这条判据顺带把「从没被跟踪过」也覆盖了：没有历史版本 → 集合为空 → 永远不匹配。
    所以它是一条规则，不是两条。
    """
    scope = ["--all"] if ref is None else [ref]
    out = subprocess.run(["git", "log", *scope, "--format=%H", "--", path],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return set()
    shas = set()
    for commit in out.stdout.split():
        blob = subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True)
        if blob.returncode == 0:
            shas.add(hashlib.sha256(blob.stdout).hexdigest())
    return shas


def git_side(ref: str) -> dict:
    """{path: sha256} for everything in that commit."""
    out = {}
    for path in run(["git", "ls-tree", "-r", "--name-only", ref]).splitlines():
        if ignored(path):
            continue
        blob = subprocess.run(["git", "show", f"{ref}:{path}"],
                              check=True, capture_output=True).stdout
        out[path] = hashlib.sha256(blob).hexdigest()
    return out


def nas_side() -> dict:
    """{path: sha256} for everything that actually sits on the NAS."""
    script = (
        f"cd {SRC} 2>/dev/null || exit 9; "
        "find . -type f -print0 | xargs -0 sha256sum 2>/dev/null"
    )
    out = {}
    for line in run(SSH + [NAS, script]).splitlines():
        digest, _, path = line.partition("  ")
        path = path[2:] if path.startswith("./") else path
        if path and not ignored(path):
            out[path] = digest
    return out


def write_status(path: str, verdict: str, detail: str) -> None:
    """写 fleet-ops SessionStart 巡检读的状态文件。

    时间戳是**这次真的查完**的时间，不是文件 mtime——巡检据它判「已停更」。
    先写临时文件再 rename：半截文件会被巡检读成「状态读不懂」，而那是一条会
    误导人的橙灯。
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(f"{at}\t{verdict}\t{detail}\n", encoding="utf-8")
    tmp.replace(p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--status-file", default=None,
                    help="把判决写成 fleet-ops 巡检的契约格式")
    ap.add_argument("--list-extra", action="store_true",
                    help="列出全部「只在 NAS 上存在」的文件（每行一个），报告用")
    ap.add_argument("--list-prunable", action="store_true",
                    help="列出其中**git 曾经跟踪、后来删掉**的那些——只有这批可以自动删")
    args = ap.parse_args()

    try:
        # fetch 失败必须当「查不了」，不能当「一致/漂了」：拿陈旧的 origin/main 去比，
        # NAS 上刚发布的新版本会被判成「内容不同」——一条查不出所以然的假红。
        fetch = subprocess.run(["git", "fetch", "origin", "--quiet"],
                               capture_output=True, text=True)
        if fetch.returncode != 0:
            msg = "拉不到 origin，主干基线是陈旧的，本次不作判决"
            print(msg, file=sys.stderr)
            if args.status_file:
                write_status(args.status_file, "error", msg)
            return 2
        want = git_side(args.ref)
        got = nas_side()
    except subprocess.CalledProcessError as exc:
        msg = f"取数失败，不作判决：{exc}"
        print(msg, file=sys.stderr)
        if args.status_file:
            # 关键：写 error 而不是 ok。「查不了」和「没漂」是两回事，
            # 混成一类会让 ssh 挂掉的那几天播报成体检通过。
            write_status(args.status_file, "error", f"取数失败：{type(exc).__name__}")
        return 2

    only_git = sorted(set(want) - set(got))
    only_nas = sorted(set(got) - set(want))          # ← 只按 git 清单查就会漏掉这批
    differs = sorted(p for p in set(want) & set(got) if want[p] != got[p])

    verdict = {
        "ref": args.ref,
        "checked": len(set(want) | set(got)),
        "missing_on_nas": only_git,
        "only_on_nas": only_nas,
        "content_differs": differs,
        "clean": not (only_git or only_nas or differs),
    }

    if args.list_extra:
        for p in only_nas:
            print(p)
        return 0 if verdict["clean"] else 1

    if args.list_prunable:
        # 「该报什么漂移」和「该删什么」**不是同一个集合**，这一点要写死在这里：
        # 报告要看见全部生产独有文件（准则 4）；而自动删只能碰**能在 git 历史里
        # 找到一模一样内容**的那些。判据写成「git 里没有的都删」会把生产打掉——
        # kg-hub 实测过 deploy/hot_config_reconciliation.py：360 行、NAS 上在跑、
        # git 里连文件名都没有（T-0084 的约束就是为这个立的）。
        #
        # 只查「曾被跟踪」还不够（kg-hub-edit 会话指出的洞）：被跟踪过 ≠ NAS 上
        # 那份还等于历史里某一版。生产上手改过、git 又删了的文件只满足前者，删了
        # 那些改动就真没了。所以比的是内容指纹，不是路径是否出现过。
        for p in only_nas:
            if got[p] in historical_hashes(p, args.ref):
                print(p)
            elif got[p] in historical_hashes(p, None):
                # 内容真实存在，只是不在这条发布线上：多半是部署不完整，或有人
                # 从别的分支拷了一份进生产。**它是信号，不是垃圾**——删掉等于把
                # 信号抹了，下次照样发生而没人知道为什么。
                print(f"跳过 {p}：这份内容在别的分支或更新的提交里能找到，但不在 "
                      f"{args.ref} 这条线上——多半是部署不完整或有人从分支拷了一份，"
                      "不是孤儿", file=sys.stderr)
            elif was_ever_tracked(p):
                # git 认识这条路径，但这份内容在任何提交里都找不到。
                print(f"跳过 {p}：曾被 git 跟踪，但这份内容在任何提交里都找不到"
                      "（疑似有人直接改过生产）", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print(f"比对 {args.ref} ↔ {NAS}:{SRC}（并集 {verdict['checked']} 个文件）")
        if verdict["clean"]:
            print("✅ 一致：线上会被执行的东西等于该 commit")
        else:
            for label, items in (("git 有、NAS 没有", only_git),
                                 ("只在 NAS 上存在", only_nas),
                                 ("内容不同", differs)):
                if items:
                    print(f"⚠️ {label}（{len(items)}）：")
                    for p in items:
                        print(f"   - {p}")

    if args.status_file:
        if verdict["clean"]:
            detail = f"{verdict['checked']} 个文件等于 {args.ref}"
        else:
            parts = []
            for label, items in (("git 有 NAS 没有", only_git),
                                 ("只在 NAS 上", only_nas),
                                 ("内容不同", differs)):
                if items:
                    parts.append(f"{label} {len(items)}：{', '.join(items[:3])}"
                                 + ("…" if len(items) > 3 else ""))
            detail = "；".join(parts)
        write_status(args.status_file, "ok" if verdict["clean"] else "drift", detail)

    return 0 if verdict["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
