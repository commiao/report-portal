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
deploy/check_source_drift.py                 # 基准 = 线上实际跑的那个 commit
deploy/check_source_drift.py --ref <commit>  # 显式指定基准
# 0=一致  1=有漂移  2=查不了（三者必须分开，混一起等于 ssh 一挂就播报体检通过）
```

**「那个 commit 还在不在主干线上」这半句判据不在本仓库**，在 fleet-ops 的
`lib/fleetops_drift.py`（T-0099 A 项收拢；本仓库和 credvault 原先各存了一份逐字
副本）。取线上指纹的方式各服务不同，那部分是适配器、留在本仓库；判据只留一处。

因此本检查器**运行期依赖 fleet-ops 产物** `~/.local/share/fleet-ops/current/lib`
（可用 `FLEET_OPS_LIB` 覆盖，只为可测）。取不到时：

- 判决落 `error`（「查不了」）+ rc=2，**不是 ok** —— 巡检会亮 🟠；
- **不回退到本地副本**（兜底等于把 bug 以兜底之名留下，只在产物缺失时发作）；
- `--list-extra/--list-prunable` **照常可用** —— 它们不需要主干判决，不该被连累，
  所以判空放在用到的那一处，不在 import 处抛。

**基准是线上实际跑的那个 commit，不是主干 tip。** 它从 NAS 的 `.env` 读
`PORTAL_IMAGE_TAG`——那是 release.sh 自己写的，不是我们猜的。写成「等于 tip」
的话，**每次发布之后到下次发布之前这条检查会一直红**（合了就红、发了才绿），
任何一次正当回滚也当场变红——而回滚恰恰是最不需要一条看不懂的红灯的时刻。

判决因此是两段：**① 线上那个 commit 在不在主干这条线上**（`merge-base
--is-ancestor`，不是「等于」——回滚到更早的 commit 是正当操作，准则 18）；
**② NAS 上的文件等不等于那个 commit**。

**拉不到 origin 时单向降级**：陈旧的 origin/main 只造成单向的错——一个 commit
若是旧主干的祖先，必然也是新主干的祖先，所以 `True` 仍可信，只有 `False` 可能是
「其实已合进去、只是这次没拉到」。于是拉不到时 True 照常放行、False 降级成第三态。
写成「拉不到就一律不判决」的话，一次网络抖动就把正常的绿变成橙，而这条链路有据
可查地会抖——天天亮的橙灯等于没有灯。

两侧都列再取并集（git 用 `ls-tree`，远端用 `find`）——**只按 git 清单查，永远看不见
「只在生产上存在」的文件**（准则 4）。首次跑就抓到 5 个：一个守卫 note + 别人留在
构建上下文里的临时备份。

> `trunk_verdict` 是从 kg-hub **逐字照抄**的（T-0099 A 项要把这段策略收进
> fleet-ops，改写它会让那件事从「合并三份相同实现」变成「论证三份不同实现等价」）。
> 只有 `live_commit()` 这个取数适配器是本服务特有的。

## prune：只删「内容能在所发 ref 的祖先链里找到」的

发布第 5.5 步清掉 git 里已不存在的文件，清单来自
`check_source_drift.py --list-prunable`。

**判据刻意不是「git 里没有的都删」**。那个补集包含**生产独有但正在用**的文件——
kg-hub 实测过 `deploy/hot_config_reconciliation.py`：360 行、NAS 上在跑、git 里连
文件名都没有，误删它就是把生产打掉。所以两个集合是分开的，而这个区别就是重点：

| 用途 | 集合 | 为什么 |
|---|---|---|
| 漂移**报告** `--list-extra` | 全部「只在 NAS 上存在」 | 看不见它们正是准则 4 要治的病 |
| 自动**删除** `--list-prunable` | 其中**内容能在所发 ref 的祖先链里找到一模一样的** | 只有这些才是这条发布线放上去、且删了也拿得回来的 |

判据比「这条路径曾被跟踪」紧两档，两档都是 kg-hub-edit 会话在复核/真机上指出的：

1. **被跟踪过 ≠ NAS 上那份还等于历史里某一版**。生产上被手改过、git 后来又删掉的
   文件只满足前者；删了它，那些改动就真没了。本仓虽有发布前整树备份兜底，但那是
   **静默**的——没人会知道去翻备份，等于悄悄毁掉一份唯一的东西。
2. **「在历史里找得到」还得限定在这条发布线上**（下面的 ⚠️）。

所以四种情形分别是：

| NAS 上这个文件 | 结果 |
|---|---|
| 内容 = **所发 ref 祖先链上**某一版 | **删**（git 历史即备份） |
| 内容在别的分支 / 更新的提交里找得到 | **不删** —— 它不是孤儿，**是部署不完整的信号**，删掉等于把信号抹了 |
| 曾被跟踪，但这份内容哪个提交里都没有 | **不删**，提示「疑似有人直接改过生产」 |
| 从没进过 git | **不删**，照常报成漂移，交人判断 |

⚠️ **范围必须是祖先链，不能是 `--all`**：`--all` 会把任何分支上的内容都算成
「历史里找得到」。kg-hub 真机上撞出来的：NAS 跑着旧 commit，目录里却有来自
**更新**提交的文件——`--all` 判它可删，可它恰恰是部署不完整的证据。同理，别人从
分支拷一份进生产，发布就会替他删掉。

这条判据顺带把「从没被跟踪过」覆盖了（没有历史版本 → 集合为空 → 永不匹配），
所以是一条规则不是两条。历史查不到时返回空集 = 不删，失败方向是安全的。

**回滚时多认一条线**：回滚会清掉「被撤销那次发布新增的」文件——它们正是那次发布
放上去的，可从它的祖先链取回（`--recoverable-from <被撤销的 tag>`）。这是**收窄**
不是放宽：只认调用方明确指出的那一个 ref，绝不是 `--all`。读不到被撤销的标签时
退化成「只按回滚目标报、不清理」。回滚同样先整树备份（`*.rollback-*.tgz`）再动手。
发布与回滚共用同一个 `prune_extras()`，判据只有一份。

安全边界：删除已被备份覆盖（发布是第 3 步、回滚是同步源码前）；每条路径删前打印；绝对路径和含 `..` 的
一律拒绝（清单是另一个命令的输出）；顺带清空目录但不碰 `$SRC` 本身。
**清单拿不到（检测退出 2）时整段跳过并明说**——没查清就不在生产上删东西。

双向实证过（`7e741cc`→`0f4342c`）：一个被 git 删掉的跟踪文件被清除；同时故意放在
NAS 上的、从没进过 git 的 `PROD_ONLY.txt` **分毫未动**，且照常报成漂移。

## 备份保留

发布与回滚各留一个整树 tgz（`*.backup-*` / `*.rollback-*`，放在 `$SRC` 的**同级**，
不在部署目录里）。保留**最近 10 份**（`PORTAL_BACKUP_KEEP` 可调），其余在发布/回滚
成功后清掉，验收行会打出「原有 N 份，删 M 份，当前占用」——不报占用的话，下次它
再涨起来仍然没人知道，而那本来就是它被漏掉的原因。

按**份数**而不是按天龄：发布是突发式的（实测几小时内 20 次），按天龄要么一次清光、
要么什么都不清。

⚠️ `$SRC/..` 是共享目录（同级住着 kg-hub-src、skill-sync-gateway、
report-portal-legacy-backups 等），所以只认我们自己那两个确切文件名前缀、删前再确认
是普通文件、绝不用宽 glob 也绝不 `rm -rf`。**列不出清单时什么都不删**——少留几十 K
比删错一份便宜得多。

> 远端脚本用 quoted heredoc 传，且**刻意不用 `case`**：macOS 自带的 bash 3.2 会把
> `$( )` 里 heredoc 中的 `;;` 误解析成语法错误（最小复现确认过，`bash -n` 当场抓到）。

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
  sha 不可变镜像、备份⊇覆盖、落地复核、回滚（含源码树同步）、漂移检测（以线上
  commit 为基准）+ 巡检接入、health 量干活、准则文档 + 机器检查、旁路封死。
- 注：kg-hub 侧的同类 prune 缺口仍开着（其 T-0084），本服务的实现可以直接抄过去。

## 加数据源/报表（回顾）

- 加**数据源**：在 `portal.py` 的 `PORTAL_SOURCES`/`_DEFAULT_SOURCES` 加一条（manifest 源
  或静态卡片源，见 `docs/MANIFEST-CONTRACT.md`）→ commit/push → `deploy/release.sh`。
- 加 **kg-hub 报表**：kg-hub 会话在它自己的 `PORTAL_REPORTS` 加卡，经 manifest 自动进门户。

## 现有源（6）

kg-hub · OpenClaw 财务 · OpenClaw 内容运营 · 跨设备工具同步(skill-sync) ·
OpenClaw 招聘情报(NAS:18180) · task-hub(:17173/ui)

任何管理 report-portal 部署的一方，请先读本文并遵守三条铁律。
