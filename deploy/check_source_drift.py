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
                    help="只列「只在 NAS 上存在」的文件（每行一个），供 release.sh "
                         "prune 使用——让「该删什么」和「报什么漂移」出自同一个定义")
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
        # 机器可读、只此一样东西：release.sh 照这份清单删。它和上面 only_nas 是
        # 同一个变量，所以「该删什么」与「报什么漂移」不可能各自演化（准则 28）。
        for p in only_nas:
            print(p)
        return 0 if verdict["clean"] else 1

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
