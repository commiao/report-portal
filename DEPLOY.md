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
| 5.5 | 清掉 git 里已没有的文件 | `tar xf` 从不删除；不清的话「等于某 commit」只在「有什么」那一半成立 |
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

## prune：删哪些不另写一份清单

发布第 5.5 步清掉「git 里已经没有」的文件，清单来自
`check_source_drift.py --list-extra`——**就是漂移检测报「只在 NAS 上存在」的那一批**。
release.sh 不自己再写一套排除规则：两份定义一旦分叉，要么删掉检测认为正常的文件，
要么检测一直报 prune 不肯删的东西（准则 28）。

安全边界：删除已被第 3 步的整树备份覆盖；每条路径删前打印；绝对路径和含 `..` 的
一律拒绝（清单是另一个命令的输出）；顺带清空目录但不碰 `$SRC` 本身。
**清单拿不到（检测退出 2）时整段跳过并明说**——没查清就不在生产上删东西，宁可留着
让漂移检测继续报。

## /health 量的是干活（准则 9）

它**真跑一遍聚合 + 渲染**（和页面走同一个 `render_portal`，准则 28），然后按
「这件事该由谁负责」分三档：

| 档 | HTTP | 什么时候 |
|---|---|---|
| `ok` | 200 | 所有源都拿到了 |
| `degraded` | **200** | 某些源不可达——那是门户如实上报的**数据**，不是它的故障 |
| `down` | 503 | 聚合/渲染抛了，或一张卡都聚合不到——门户自己干不了活 |

`degraded` 刻意不 503：否则别人家面板停机会让 release.sh 把门户自动回滚掉。
返回体给真数出来的 `cards` 数、`sources.failed` 列表、渲染字节数和耗时。

**它能抓到的一种静默故障**：模板里把 `__DATA__` 占位符改名后，`.replace` 是一次
静默无操作——页面照常 200、结构完整、一张卡都没有。字符串替换不报错，所以
`render_portal` 自己校验「数据真的嵌进去了」，不然就抛（页面也一起受保护，
而不是端出一个空白的 200）。

## 漂移巡检（SessionStart 自动摆出来）

```sh
deploy/mac/install-drift-probe.sh     # 装 launchd 日更探针（幂等）
```
判决写 `~/.cache/report-portal/source-drift.status`，fleet-ops 的
`ops-hook-context.sh` 每次开会话读它。探针跑 `~/.local/share/report-portal/repo`
这个**私有 clone**，不指向共享开发工作树（准则 20）。`release.sh` 成功后会自己
刷新判决（准则 10）。

## 已知缺口（诚实记账）

- 暂无。逐服务契约表本服务已全绿：源码=commit、只发主干闸、git archive、prune、
  sha 不可变镜像、备份⊇覆盖、落地复核、回滚、漂移检测+巡检接入、health 量干活、
  准则文档 + 机器检查、旁路封死。
- 注：kg-hub 侧的同类 prune 缺口仍开着（其 T-0084），本服务的实现可以直接抄过去。

## 加数据源/报表（回顾）

- 加**数据源**：在 `portal.py` 的 `PORTAL_SOURCES`/`_DEFAULT_SOURCES` 加一条（manifest 源
  或静态卡片源，见 `docs/MANIFEST-CONTRACT.md`）→ commit/push → `deploy/release.sh`。
- 加 **kg-hub 报表**：kg-hub 会话在它自己的 `PORTAL_REPORTS` 加卡，经 manifest 自动进门户。

## 现有源（6）

kg-hub · OpenClaw 财务 · OpenClaw 内容运营 · 跨设备工具同步(skill-sync) ·
OpenClaw 招聘情报(NAS:18180) · task-hub(:17173/ui)

任何管理 report-portal 部署的一方，请先读本文并遵守三条铁律。
