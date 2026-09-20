# report-portal 部署约定（单一真源，防多 actor 漂移）

> 多方（多个 Claude/Codex 会话）都会碰 report-portal。曾发生：两方用**不同 compose
> 项目名 + 直接手改 NAS 共享目录**，导致两容器抢 :17172、互相覆盖源、有源只在运行
> 容器里没进 git。以下约定是为了**永久消除**这种漂移。

## 三条铁律

1. **唯一真源 = 这个 git 仓**：`git@github-commiao:commiao/report-portal.git`（main）。
   所有改动先改这里、`git commit && git push`，再部署。
   **绝不直接手改 NAS 上的 `/volume1/docker/report-portal-src/portal.py`**（那是部署
   落地目录，不是编辑处；手改会被下一次同步覆盖、且不进 git = 丢失）。

2. **唯一 compose 项目名 = `report-portal-src`**（= NAS 源码目录名，也是线上正在跑的
   项目名）。任何 `docker compose` 命令都用 `-p report-portal-src`。
   **别再用 `-p report-portal`** —— 不同项目名会各建容器、抢宿主 :17172。

3. **部署只走 `deploy/release.sh`**。`deploy/redeploy.sh` 已停用（它从工作树直发，
   线上版本无法追溯到 commit）——留着旁路，下面所有闸门就都是可选的（准则 21）。

## 发布（deploy/release.sh）

```sh
deploy/release.sh              # 发布 origin/main
deploy/release.sh --dry-run    # 只跑闸门，不动 NAS
deploy/release.sh --sha <c>    # 发布指定 commit（必须在主干上）
deploy/release.sh rollback <sha>   # 回滚到盘上已有的镜像标签
```

它按 deploy-standard 做七步：

| 步 | 做什么 | 防的是 |
|---|---|---|
| 1 | `merge-base --is-ancestor <sha> origin/main` | 准则 18：从分支直发，下一个从 main 出发的人会把它悄悄抹掉 |
| 2 | 记下当前标签作回滚点 | 准则 6/7 |
| 3 | 整棵源目录 tgz 备份 | 准则 5：备份 ⊇ 覆盖（archive 覆盖整树） |
| 4 | `git archive <sha>` 落地 | 准则 1：发布物按定义等于那个 commit |
| 5 | 逐文件 sha256 复核 | 准则 26：不是「我传上去了」，是可机器验证的等于 |
| 6 | 构建 `report-portal:<sha>`（不动 latest） | 准则 26：内容不可变 |
| 7 | 镜像来源比对 + 健康检查，不合格自动回上一个标签 | 准则 6 |

镜像标签由 release.sh 写进 NAS 上的 `.env`（`PORTAL_IMAGE_TAG`）；compose 里用
`${PORTAL_IMAGE_TAG:?}`，**没人指定版本时 compose 当场失败**而不是悄悄跑 latest。

## 漂移检测

```sh
deploy/check_source_drift.py          # 比 origin/main，0=一致 1=有漂移 2=取数失败
```
两侧都列再取并集（git 用 `ls-tree`，远端用 `find`）——**只按 git 清单查，永远看不见
「只在生产上存在」的文件**（准则 4）。首次跑就抓到 5 个：一个守卫 note + 别人留在
构建上下文里的临时备份。

## 已知缺口（诚实记账）

- **`git archive` 不删除**：git 里删掉的文件会留在 NAS 上（kg-hub 同病，其 T-0084）。
  漂移检测能抓到（`只在 NAS 上存在`），但 release 本身不 prune。
- **`/health` 量活着不量干活**（准则 9）：它只回配置里的源 id，不校验源真的抓得到。
  页面上每个源有红绿点，但 health 端点本身不体现。
- **巡检未接入 SessionStart**：kg-hub / credvault 的漂移检测挂在 fleet-ops 的
  hook 上，本服务还要手工跑。

## 加数据源/报表（回顾）

- 加**数据源**：在 `portal.py` 的 `PORTAL_SOURCES`/`_DEFAULT_SOURCES` 加一条（manifest 源
  或静态卡片源，见 `docs/MANIFEST-CONTRACT.md`）→ commit/push → `deploy/release.sh`。
- 加 **kg-hub 报表**：kg-hub 会话在它自己的 `PORTAL_REPORTS` 加卡，经 manifest 自动进门户。

## 现有源（6）

kg-hub · OpenClaw 财务 · OpenClaw 内容运营 · 跨设备工具同步(skill-sync) ·
OpenClaw 招聘情报(NAS:18180) · task-hub(:17173/ui)

任何管理 report-portal 部署的一方，请先读本文并遵守三条铁律。
