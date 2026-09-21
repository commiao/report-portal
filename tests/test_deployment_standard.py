"""report-portal 的部署准则机器检查。

准则文档会被绕过，因为它不参与任何判断；带机器检查的准则才有效。这个文件把
deploy-standard 里适用于本服务的几条，变成会当场变红的断言。

每个用例的 docstring 写它防的是哪一条、以及那条是怎么被踩出来的。
"""
import hashlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _executable(script: str) -> str:
    """只留会执行的行。

    变异验证时抓到的真实缺陷：release.sh 的注释里也写着 `merge-base --is-ancestor`
    和 `git archive`，于是把真正的闸门整行删掉、只留注释，`assertIn` 照样是绿的
    ——正是准则 17 说的「删掉被测代码仍然通过」。断言必须只看代码。
    """
    return "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))


RELEASE = _executable((ROOT / "deploy" / "release.sh").read_text(encoding="utf-8"))
COMPOSE = _executable((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
REDEPLOY = _executable((ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8"))
DRIFT = _executable((ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8"))


class ReleasePathTests(unittest.TestCase):
    def test_only_publishes_commits_on_the_trunk(self):
        """准则 18：从分支直发，线上那份只存在于那条分支上，下一个从 main 出发的
        人会把它悄悄抹掉且没有冲突提示。用 is-ancestor 而不是「等于 origin/main」，
        因为回滚到更早的 commit 是正当操作。"""
        self.assertIn("merge-base --is-ancestor", RELEASE)
        self.assertIn("origin/main", RELEASE)
        # 闸必须真的拦住：不通过就得退出，而不是打印一行继续发。
        gate = next(l for l in RELEASE.splitlines() if "merge-base --is-ancestor" in l)
        idx = RELEASE.splitlines().index(gate)
        self.assertIn("die", "\n".join(RELEASE.splitlines()[idx:idx + 2]))

    def test_artifact_is_a_commit_not_the_working_tree(self):
        """准则 1：发布物必须等于某个 commit。`cat 工作树文件 | ssh` 会把本机任何
        未提交的改动一起发上去，于是「线上是哪个 commit」没有答案。"""
        # 只看投放那一步（[4/7] 段）。回滚里也有一处 archive——那是 T-0101 之后
        # 「回滚要把源码树也同步回去」的正当用法，不该把这条断言顶红。
        publish_block = RELEASE.split("[4/7]", 1)[1].split("[5/7]", 1)[0]
        publish = [l for l in publish_block.splitlines() if "archive --format=tar" in l]
        self.assertEqual(len(publish), 1, "投放必须且只能走 git archive")
        self.assertNotIn("cat \"$REPO/$f\"", RELEASE)

    def test_backs_up_the_whole_tree_before_overwriting(self):
        """准则 5：备份范围 ⊇ 覆盖范围。git archive 覆盖整棵树，所以只备份「构建
        输入」那几个文件的话，第一次真跑就会把生产独有文件盖掉——kg-hub 踩过。"""
        self.assertIn("tar czf", RELEASE)
        self.assertIn("-C '$SRC' .", RELEASE)

    def test_verifies_what_actually_landed(self):
        """准则 26：不是「我传上去了」，是可机器验证的等于——落地后逐文件比哈希。"""
        self.assertIn("ls-tree -r --name-only", RELEASE)
        self.assertIn("sha256sum", RELEASE)
        self.assertIn("落地后与", RELEASE)

    def test_prunes_files_git_no_longer_has(self):
        """`git archive | tar xf` 只覆盖和新增，从不删除。不清的话，git 里删掉的
        文件会一直留在 NAS 上并可能被执行——「线上等于某个 commit」就只在「有
        什么」这一半成立（kg-hub 的 T-0084 至今开着）。"""
        self.assertIn("--list-prunable", RELEASE)
        self.assertIn("rm -f --", RELEASE)

    def test_prune_never_deletes_on_the_bare_extras_list(self):
        """T-0084 立的约束：**不要把判据写成「git 里没有的都删」**。
        那个补集包含生产独有但正在用的文件——kg-hub 实测过
        deploy/hot_config_reconciliation.py：360 行、NAS 上在跑、git 里连文件名
        都没有，误删它就是把生产打掉。

        所以「报什么漂移」和「该删什么」**刻意不是同一个集合**：报告要看见全部
        生产独有文件（准则 4），删除只能碰 git 曾经跟踪、后来删掉的那些。
        """
        # prune 已抽成 prune_extras()（发布与回滚共用一份），断言钉函数体
        prune_block = RELEASE.split("prune_extras() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("--list-prunable", prune_block)
        self.assertNotIn("--list-extra", prune_block)
        # 而收窄的判据必须真的基于 git 历史，不是又一份手写排除清单
        self.assertIn("def was_ever_tracked", DRIFT)
        self.assertIn("git", DRIFT)
        for own_rule in ("IGNORE_EXACT", "IGNORE_PREFIX", "IGNORE_SUFFIX"):
            self.assertNotIn(own_rule, RELEASE)

    def test_prune_refuses_paths_that_escape_the_deploy_dir(self):
        """删除是不可逆的，清单又来自外部命令的输出。"""
        self.assertIn("*..*", RELEASE)
        self.assertIn("拒绝删除可疑路径", RELEASE)

    def test_a_failed_listing_does_not_read_as_nothing_to_prune(self):
        """空清单和「这次没查成」混成一句，取数一挂就会播报成「清理干净」。
        没查清时宁可不删——留着让漂移检测继续报。

        断言那个**比较真的在跑**，不只是这些字眼出现过：变异验证时把分支改成
        `if false` 之后，`EXTRA_RC` 和那句提示都还原样留在文件里，只查字符串
        在不在的写法当场放行了它。
        """
        prune_fn = RELEASE.split("prune_extras() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("rc=$?", prune_fn)                       # 真取了退出码
        # 判据必须是「非 0」，不能是「等于某个预料中的码」。第一版写成 `= "2"`，
        # 只堵住了检测自己主动 return 2 的那种失败；检查器崩掉（未捕获异常）给的
        # 是 rc=1，$EXTRA 同样为空，于是直接落进「没有多余文件」——正是本用例要
        # 防的那句话。实测 python 未捕获异常退出码就是 1。
        self.assertIn('[ "$rc" != "0" ]', prune_fn)
        # 而且这个判断必须排在「没有多余文件」那句之前，否则查不了会先被归成「干净」
        self.assertLess(prune_fn.index('[ "$rc" != "0" ]'), prune_fn.index("没有多余文件"))

    def test_releases_are_serialised(self):
        """两个发布交错会互相删文件——第 5.5 步现在会**删**东西，而这个仓有明确的
        多 actor 撞车史。形态很具体：A 刚 archive 落地还没走完，B 的 prune 看到那些
        文件不在自己那个 ref 的祖先链上，就把 A 刚放上去的删了。"""
        self.assertIn("mkdir '$LOCK'", RELEASE)        # mkdir 是原子的
        self.assertIn("trap release_lock EXIT", RELEASE)
        # 发布和回滚都改 NAS，两条路径都必须串行化
        self.assertEqual(RELEASE.count("acquire_lock\n") + RELEASE.count("acquire_lock  "), 2)

    def test_the_lock_is_taken_before_anything_on_the_nas_changes(self):
        """锁要是排在备份/落地之后，加它就没意义了。

        两条路径各自判：回滚现在也取锁、也备份，用「文件里第一次出现」定位会串味。
        锚点只用代码，不用注释——RELEASE 是滤掉注释行的，拿 `# ---- 2. 取锁` 当
        锚点会直接 ValueError（第一版就是这么写的）。
        """
        # 发布路径：取锁 → 备份
        self.assertLess(RELEASE.index("acquire_lock\nPREV="),
                        RELEASE.index('BACKUP="$SRC/../report-portal-src.backup-'))
        # 回滚路径：取锁 → 备份（现在它也会删文件，所以也必须先备份）
        rb = RELEASE.split('if [ "$MODE" = "rollback" ]; then', 1)[1]
        self.assertLess(rb.index("acquire_lock"),
                        rb.index("report-portal-src.rollback-"))

    def test_only_releases_a_lock_it_actually_took(self):
        """抢占失败时若照样 rm，等于把**别人正在用的**锁删掉——比没有锁更糟。"""
        body = RELEASE.split("release_lock() {", 1)[1].split("}", 1)[0]
        self.assertIn('[ "$lock_acquired" = 1 ] || return 0', body)

    def test_a_stale_lock_can_be_taken_over(self):
        """一次崩溃就把发布路径永久堵死，比并发更糟。"""
        self.assertIn("-mmin +40", RELEASE)

    def test_rollback_backs_up_before_it_deletes(self):
        """回滚现在会删文件（清掉被撤销那次发布新增的），所以它也必须先整树备份
        ——准则 5 的「覆盖」现在含删除。原来回滚根本没有备份这一步。"""
        rb = RELEASE.split('if [ "$MODE" = "rollback" ]; then', 1)[1]
        self.assertIn("report-portal-src.rollback-", rb)
        self.assertLess(rb.index("tar czf"), rb.index("prune_extras"))
        # 备份失败就不许继续动源码树
        self.assertIn("备份失败，不继续动源码树", rb)

    def test_rollback_records_what_it_is_undoing_before_overwriting_env(self):
        """清残留要靠「被撤销的是哪一次」，而那个值就存在马上要被覆盖的 .env 里。
        顺序错了就永远读不到，清理只能退化成不清。"""
        rb = RELEASE.split('if [ "$MODE" = "rollback" ]; then', 1)[1]
        self.assertLess(rb.index("UNDOING=$("), rb.index("PORTAL_IMAGE_TAG=%s"))

    def test_rollback_widens_recoverability_to_one_named_ref_not_all(self):
        """回滚多认一条线是**收窄**不是放宽：只认调用方明确指出的那个 ref
        （被撤销的那次发布），绝不是 `--all`——后者会把任何分支都算成可删。"""
        rb = RELEASE.split('if [ "$MODE" = "rollback" ]; then', 1)[1]
        self.assertIn('prune_extras "$ROLLBACK_SHA" "$UNDOING"', rb)
        self.assertNotIn("--all", RELEASE)
        # 读不到被撤销的标签时降级成「只按回滚目标报」，而不是乱删
        self.assertIn('prune_extras "$ROLLBACK_SHA"\n', rb)

    def test_prune_logic_exists_once(self):
        """发布和回滚共用一份 prune。抄两遍的话判据改一次要改两处——而这套东西的
        全部价值就在判据上（T-0099 抱怨的正是「共享逻辑抄了四遍」）。"""
        self.assertEqual(RELEASE.count("prune_extras() {"), 1)
        self.assertEqual(RELEASE.count("--list-prunable"), 1)   # 只在函数里出现一次
        # 删除动作各自只在自己的函数里出现一次（备份保留是另一个删除器，别混着数）
        for fn_name in ("prune_extras() {", "prune_backups() {"):
            body = RELEASE.split(fn_name, 1)[1].split("\n}", 1)[0]
            self.assertEqual(body.count("rm -f --"), 1, fn_name)


    def test_has_a_rollback_path(self):
        """准则 6/7：带着没有退路的发布切生产，就是 kg-hub「切了之后回不来」那种
        形态。本服务无状态，回滚 = 标签指回去再 up。"""
        self.assertIn("rollback", RELEASE)
        self.assertIn("自动回滚", RELEASE)
        self.assertIn("--no-build", RELEASE)


class ImmutableImageTests(unittest.TestCase):
    def test_compose_refuses_to_run_an_unpinned_image(self):
        """准则 1/26：给默认值会让没人指定版本时悄悄跑成 latest，线上是哪个 commit
        就再也说不清。用 `:?` 让 compose 当场失败。"""
        self.assertIn("${PORTAL_IMAGE_TAG:?", COMPOSE)
        self.assertNotIn("image: report-portal:latest", COMPOSE)


class BypassTests(unittest.TestCase):
    def test_the_old_working_tree_path_is_closed(self):
        """准则 21：绕过发布路径改一次生产，正式发布路径本身也会失效。只要旁路还
        在，闸门/备份/复核/回滚就全可以被绕过，那它们等于不存在。"""
        self.assertIn("exit 2", REDEPLOY)
        self.assertIn("已停用", REDEPLOY)
        self.assertNotIn("compose -p", REDEPLOY)


class PruneContentGateTests(unittest.TestCase):
    """删除的判据是「NAS 上这份内容能在 git 历史里找到一模一样的」，
    比「这条路径曾被跟踪」紧一档。

    紧的那一档是 kg-hub-edit 会话指出的洞：被跟踪过 ≠ NAS 上那份还等于历史里
    某一版。生产上被手改过、git 后来又删掉的文件只满足前者；删了它，那些改动
    就真没了（本仓有发布前整树备份兜底，但那是静默的——没人会知道去翻）。
    """

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "portal_drift2", ROOT / "deploy" / "check_source_drift.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_path_git_never_had_has_no_recoverable_content(self):
        """从没进过 git 的文件——生产独有、可能正在用——永远不会进可删清单。"""
        self.assertEqual(self._module().historical_hashes("NEVER_EXISTED_xyz.py"), set())

    def test_a_tracked_path_exposes_its_historical_versions(self):
        self.assertTrue(self._module().historical_hashes("portal.py"))

    def test_content_never_committed_is_not_recoverable(self):
        """路径认识、内容不认识——这正是「有人直接改了生产」的形状。"""
        edited = hashlib.sha256(b"SOMEONE HAND-EDITED PRODUCTION\n").hexdigest()
        self.assertNotIn(edited, self._module().historical_hashes("portal.py"))

    def _temp_repo(self, tmp):
        """造一个真 git 仓：main 上 A→C，side 分支上 B。返回三份内容的 sha256。

        自带 origin（指向自己）以便 main() 里的 `git fetch origin` 能过——不为
        测试在生产代码里开后门。
        """
        import subprocess as sp
        run = lambda *a: sp.run(a, cwd=tmp, check=True, capture_output=True)
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        out = {}
        for name, content, branch in (("A", b"A\n", None), ("B", b"B\n", "side"),
                                      ("C", b"C\n", "main")):
            if branch == "side":
                run("git", "checkout", "-qb", "side")
            elif branch == "main":
                run("git", "checkout", "-q", "main")
            (pathlib.Path(tmp) / "f.txt").write_bytes(content)
            run("git", "add", "-A")
            run("git", "commit", "-qm", name)
            out[name] = hashlib.sha256(content).hexdigest()
        run("git", "remote", "add", "origin", tmp)
        return out

    def test_historical_hashes_is_scoped_to_the_ref_ancestry(self):
        """`--all` 会把**任何分支**上的内容都算成「历史里找得到」。真机上撞出来的
        形态（kg-hub-edit）：NAS 跑着旧 commit，目录里却有来自**更新**提交的文件
        ——它不是孤儿，是**部署不完整的信号**，删掉等于把信号抹了。"""
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)
            # git 调用已统一到 `-C REPO`，不再看 cwd —— 所以这里换掉 REPO 而不是 chdir。
            old = mod.REPO
            try:
                mod.REPO = pathlib.Path(tmp)
                anc = mod.historical_hashes("f.txt", "main")
                everywhere = mod.historical_hashes("f.txt", None)
            finally:
                mod.REPO = old
        self.assertIn(sha["A"], anc)
        self.assertIn(sha["C"], anc)
        self.assertNotIn(sha["B"], anc)          # 侧分支的内容不在 main 的祖先链上
        self.assertIn(sha["B"], everywhere)      # 但 --all 看得见它（只用于分类）

    def test_list_prunable_uses_the_release_ref_not_all(self):
        """光证明函数对没用——还得证明**有人在用它，并且传对了 ref**。
        （kg-hub-edit 的变异验证里，「把判据退回 was_ever_tracked」没转红，就是因为
        用例只验了函数本身、从没跑过调用点。）这里真跑 main()。"""
        from unittest import mock
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)
            old = mod.REPO
            try:
                mod.REPO = pathlib.Path(tmp)
                with mock.patch.object(mod, "git_side", lambda ref: {}), \
                     mock.patch.object(mod, "nas_side", lambda: {"f.txt": sha["B"]}), \
                     mock.patch.object(sys, "argv",
                                       ["x", "--list-prunable", "--ref", "main"]), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                     mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    rc = mod.main()
            finally:
                mod.REPO = old
        self.assertEqual(rc, 0)
        # 侧分支的内容不该进可删清单……
        self.assertEqual(out.getvalue().strip(), "")
        # ……而且要说清它为什么被跳过，措辞不能写成「有人改了生产」
        self.assertIn("不在 main 这条线上", err.getvalue())

    def test_recoverable_from_widens_to_exactly_one_extra_line(self):
        """真跑：side 分支上的内容，只有在把 side 显式指为 --recoverable-from 时
        才算可取回；不给时不算，给 --all 那种放宽更是绝不允许。"""
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)          # main: A→C, side: B
            old = mod.REPO
            try:
                mod.REPO = pathlib.Path(tmp)
                self.assertNotIn(sha["B"], mod.historical_hashes("f.txt", "main"))
                self.assertIn(sha["B"], mod.historical_hashes("f.txt", "side"))
            finally:
                mod.REPO = old


    def test_list_prunable_actually_honours_recoverable_from(self):
        """真跑调用点，不只验函数——变异「让 recoverable() 无视 --recoverable-from」
        时，只验 historical_hashes 的用例全绿（回滚残留永远清不掉却没人发现）。
        这正是本项目记过的那条：函数正确 ≠ 有人在用它、且用对了。"""
        from unittest import mock
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)          # main: A→C；side: B
            old = mod.REPO
            try:
                mod.REPO = pathlib.Path(tmp)
                # NAS 上躺着一份只存在于 side 的内容，基准是 main
                patches = (mock.patch.object(mod, "git_side", lambda ref: {}),
                           mock.patch.object(mod, "nas_side", lambda: {"f.txt": sha["B"]}))

                def run_with(argv):
                    with patches[0], patches[1], \
                         mock.patch.object(sys, "argv", argv), \
                         mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                         mock.patch("sys.stderr", new_callable=io.StringIO):
                        mod.main()
                        return out.getvalue().strip()

                # 不给额外的线：不可删（它不在 main 的祖先链上）
                self.assertEqual(run_with(["x", "--list-prunable", "--ref", "main"]), "")
                # 显式指出「被撤销的那次」= side：这才算可取回，应进清单
                self.assertEqual(
                    run_with(["x", "--list-prunable", "--ref", "main",
                              "--recoverable-from", "side"]), "f.txt")
            finally:
                mod.REPO = old


    def test_the_gate_compares_content_not_just_the_path(self):
        """判据必须是内容是否可在历史中找到，不能退回成「这条路径曾被跟踪」。

        断言的是**放行那一行本身**，不是「这些字眼在不在」：第一版写成
        `assertNotIn("if was_ever_tracked", block)`，而代码里的 `elif
        was_ever_tracked` 恰好包含这个子串，测试当场自己红了——同一天里
        `assertIn` 的子串语义已经第三次咬人（前两次是 `.v-tile .gridX` 和
        注释里的 `merge-base --is-ancestor`）。
        """
        drift_src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        block = drift_src.split("if args.list_prunable:", 1)[1].split("if args.json:", 1)[0]
        # 放行走 recoverable() 这层间接，所以要连它的定义一起看
        helper = block.split("def recoverable", 1)[1].split("for p in only_nas:", 1)[0]
        self.assertIn("historical_hashes", helper)    # 放行看的是内容能否在祖先链里找到
        self.assertNotIn("was_ever_tracked", helper)  # 「曾被跟踪」只配解释为什么跳过
        loop = block.split("for p in only_nas:", 1)[1]
        gate = next(l.strip() for l in loop.splitlines() if l.strip().startswith("if "))
        self.assertIn("recoverable(", gate)


class DriftStatusContractTests(unittest.TestCase):
    """状态文件是 fleet-ops SessionStart 巡检读的契约，这里按它的解析方式实跑。

    上面几条是静态断言（文本里有没有那一句）；这几条是真的调函数、真的解析，
    因为格式写错不会有人发现——巡检只会显示成一条橙灯「状态读不懂」。
    """

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "portal_drift", ROOT / "deploy" / "check_source_drift.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _parse(self, path):
        """完全照 ops-hook-context.sh 的读法：三段 tab 分隔 + ISO 时间。"""
        at, verdict, detail = pathlib.Path(path).read_text("utf-8").strip().split("\t", 2)
        datetime.fromisoformat(at.replace("Z", "+00:00"))  # 解析不了就当场抛
        return verdict, detail

    def test_status_file_matches_what_the_sweep_parses(self):
        mod = self._module()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "sub" / "source-drift.status"   # 父目录不存在也要能写
            mod.write_status(str(p), "ok", "16 个文件等于 origin/main")
            verdict, detail = self._parse(p)
        self.assertEqual(verdict, "ok")
        self.assertEqual(detail, "16 个文件等于 origin/main")

    def test_a_failed_check_is_not_written_as_ok(self):
        """准则：「查不了」和「没漂」是两回事。写成 ok 的话，ssh 挂掉的那几天
        SessionStart 会一直播报体检通过。"""
        mod = self._module()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s"
            mod.write_status(str(p), "error", "取数失败：CalledProcessError")
            verdict, _ = self._parse(p)
        self.assertNotEqual(verdict, "ok")
        self.assertNotEqual(verdict, "drift")   # 也不能混进「漂了」那一类


class DriftDetectionTests(unittest.TestCase):
    def test_lists_both_sides_and_takes_the_union(self):
        """准则 4：只按 git 清单去查，永远看不见「只在生产上存在」的文件——
        kg-hub 的 hot_config_reconciliation.py 360 行在生产跑着、git 里没有，
        而当时的检测显示「✅ 对得上」。"""
        self.assertIn("ls-tree", DRIFT)
        self.assertIn("find . -type f", DRIFT)
        self.assertIn("set(got) - set(want)", DRIFT)

    def test_a_failed_lookup_is_not_reported_as_clean(self):
        """取数失败必须和「一致」区分开，否则 ssh 挂掉会被读成体检通过。"""
        self.assertIn("取数失败，不作判决", DRIFT)
        self.assertIn("return 2", DRIFT)


class BackupRetentionTests(unittest.TestCase):
    """备份是准则 5 要的，保留策略是它的配套——没有的话它无界增长，
    而且**没人会发现它在涨**（实测一天 20 个）。"""

    def _fn(self):
        return RELEASE.split("prune_backups() {", 1)[1].split("\n}", 1)[0]

    def test_retention_is_by_count_not_age(self):
        """发布是突发式的（几小时内 20 次）。按天龄要么一次清光、要么什么都不清；
        按份数才保证「总能回退最近 N 次」。"""
        fn = self._fn()
        self.assertIn("PORTAL_BACKUP_KEEP:-10", fn)
        self.assertIn("tail -n +$((keep + 1))", fn)
        self.assertNotIn("-mtime", fn)

    def test_it_only_ever_matches_our_own_two_filenames(self):
        """`$SRC/..` 是共享目录：同级住着 kg-hub-src、skill-sync-gateway、
        report-portal-legacy-backups。宽 glob 会删到别人家。"""
        fn = self._fn()
        self.assertIn("report-portal-src.backup-*.tgz", fn)
        self.assertIn("report-portal-src.rollback-*.tgz", fn)
        # 删除前还要再确认是普通文件——绝不碰目录
        self.assertIn('[ -f "$f" ] || continue', fn)
        self.assertIn("rm -f --", fn)
        self.assertNotIn("rm -rf", fn)

    def test_a_failed_listing_keeps_everything(self):
        """少删一份只是多占几十 K，删错一份不可逆。所以拿不到清单时什么都不删，
        而不是「没列出来就当没有」。"""
        fn = self._fn()
        self.assertIn("grep -q '^TOTAL='", fn)
        self.assertIn("本次不清理", fn)
        # 守卫必须排在统计/汇报之前
        self.assertLess(fn.index("本次不清理"), fn.index("原有 $total 份"))

    def test_both_paths_share_one_retention(self):
        """发布和回滚都会造备份，都得清；抄两遍则两边各自漂。"""
        self.assertEqual(RELEASE.count("prune_backups() {"), 1)
        calls = [l for l in RELEASE.splitlines() if l.strip() == "prune_backups"]
        self.assertEqual(len(calls), 2, "发布与回滚各调一次")

    def test_usage_is_reported_so_growth_is_visible(self):
        """不报占用的话，下次它再涨起来仍然没人知道——这本来就是它被漏掉的原因。"""
        self.assertIn("当前占用", self._fn())

    def test_remote_script_avoids_case_in_a_heredoc(self):
        """macOS 自带 bash 3.2 会把 `$( )` 里 heredoc 中的 `;;` 误解析成语法错误。
        最小复现：同一段 heredoc 去掉 case 就正常。`bash -n` 当场抓到过。"""
        fn = self._fn()
        self.assertNotIn(";;", fn)



class DeployedCommitBaselineTests(unittest.TestCase):
    """基准是**线上实际跑的那个 commit**，不是主干 tip（T-0101 / T-0099 B）。

    写成「等于 tip」的话，每次发布之后到下次发布之前这条检查会一直红（合了就红、
    发了才绿），任何一次正当回滚也当场变红——而回滚恰恰是最不需要一条看不懂的
    红灯的时刻。
    """

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "portal_drift3", ROOT / "deploy" / "check_source_drift.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_default_baseline_is_the_live_tag_not_the_trunk_tip(self):
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        self.assertIn('ap.add_argument("--ref", default=None', src)
        self.assertIn("args.ref or live_commit()", src)
        # 取数适配器读的必须是 release.sh 自己写的那个标记
        self.assertIn("PORTAL_IMAGE_TAG", src)

    def test_does_not_write_bytecode_into_fleet_ops_release_dir(self):
        """别把 __pycache__ 写进人家的发布产物目录。

        fleet-ops 的 releases/<sha>/ 契约是**内容不可变**——原子切换就是靠这一点
        成立的。2026-09-21 实测：接上之后第一次跑就在 current/lib/ 下留了个
        __pycache__，那份产物于是不再逐字等于它的 commit。**而这个脚本自己就是
        来抓这种事的。**

        真跑，不读源码：造一个假的 fleet-ops lib 目录指过去，跑完看它干净不干净。
        """
        import shutil, subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            lib = pathlib.Path(tmp) / "lib"
            lib.mkdir()
            real = pathlib.Path.home() / ".local/share/fleet-ops/current/lib/fleetops_drift.py"
            if not real.exists():
                self.skipTest("本机没有 fleet-ops 产物")
            shutil.copy(real, lib / "fleetops_drift.py")
            sp.run([sys.executable, str(ROOT / "deploy" / "check_source_drift.py"),
                    "--ref", "HEAD", "--list-prunable"],
                   capture_output=True, text=True, cwd=str(ROOT), timeout=60,
                   env=dict(os.environ, FLEET_OPS_LIB=str(lib)))
            self.assertEqual(sorted(p.name for p in lib.iterdir()),
                             ["fleetops_drift.py"], "产物目录被写脏了")

    def test_probe_updates_its_checkout_before_running(self):
        """探针必须先把工作树切到主干，再跑检查器。

        `git fetch` **不动工作树**：装上那天检出的是哪个 commit，之后就一直跑
        哪个。2026-09-21 实测私有克隆停在 af6439c，而主干早已走远 —— 于是这个
        探针天天写出新鲜的时间戳、用的却是装机那天的逻辑。它自己就是来治
        「新鲜时间戳盖着陈旧结论」的，结果病在它自己身上。

        两种失败要分开，所以这里分别钉：fetch 失败照跑（本地 origin/main 还在，
        陈旧只造成单向的错），checkout 失败 exit 2（克隆真坏了，此时任何结论都
        不知道是哪一版算的）。
        """
        import plistlib
        plist = ROOT / "deploy" / "mac" / "agents" / "com.report-portal.source-drift.plist"
        cmd = plistlib.loads(plist.read_bytes())["ProgramArguments"][2]
        self.assertIn("checkout -q --detach origin/main", cmd)
        self.assertLess(cmd.index("checkout -q --detach"), cmd.index("check_source_drift.py"),
                        "切主干必须在跑检查器之前")
        # fetch 后面不许跟 || exit —— 抖一下不该变成橙灯
        fetch_tail = cmd.split("git fetch origin --quiet", 1)[1].split(";", 1)[0]
        self.assertNotIn("exit", fetch_tail, "fetch 失败应当照跑")
        # checkout 后面必须跟 || exit 2
        co_tail = cmd.split("checkout -q --detach origin/main", 1)[1].split(";", 1)[0]
        self.assertIn("|| exit 2", co_tail, "checkout 失败必须停下")

    def test_release_refuses_to_run_under_sh_or_zsh(self):
        """用错 shell 要在**动 NAS 之前**就停，不是跑到一半再炸。

        2026-09-21 的真实代价：`sh deploy/release.sh` 死在第 5 步的进程替换上，
        而 bash 边解析边执行，前四步已经跑完——备份做了、archive 落地了、镜像
        标签还没换。生产停在「文件是新 commit、跑着的是旧镜像」的半发布态。

        两个条件都要查：macOS 的 sh 就是 bash 的 POSIX 模式，**照样设
        BASH_VERSION**（实测 3.2.57）。只查 BASH_VERSION 的话，最该拦的那个
        调用方式恰好被放行——这正是「把我想到的情况当成全部情况」。

        探针用一个非法参数：它 4 毫秒返回、不打网络也不抢发布锁，而参数解析在
        闸**之后**（脚本第 214 行 vs 第 30 行）。于是 rc 正好把两者分开——
        拿到 2 说明闸先响了，拿到 1 说明闸被绕过去、参数解析先跑了。
        """
        import subprocess as sp
        script = str(ROOT / "deploy" / "release.sh")
        for shell in ("sh", "zsh"):
            with self.subTest(shell):
                proc = sp.run([shell, script, "--no-such-flag"],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=60)
                both = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 2,
                                 f"{shell} 应被闸拦下（rc=1 说明闸没先跑）: {both[:200]}")
                self.assertNotIn("[1/7]", both, "拒绝时不该已经开始动 NAS")
                self.assertNotIn("未知参数", both, "参数解析不该跑在闸前面")

    def test_release_guard_does_not_refuse_real_bash(self):
        """闸的另一侧：别关过头。拦住 bash 的话，发布路径本身就没了。

        同一个非法参数探针：真 bash 应当**穿过闸**、走到参数解析才失败（rc=1）。
        """
        import subprocess as sp
        proc = sp.run(["bash", str(ROOT / "deploy" / "release.sh"), "--no-such-flag"],
                      capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        both = proc.stdout + proc.stderr
        for shouted in ("必须用 bash 跑", "别用 sh 跑"):
            self.assertNotIn(shouted, both)
        self.assertEqual(proc.returncode, 1, both[:200])
        self.assertIn("未知参数", both)

    def test_no_local_copy_of_the_trunk_judgement(self):
        """判据只留一处（准则 3/4）。本地再长出一份，收拢就悄悄自我撤销了。

        钉的是 `def`，不是字眼：注释和 docstring 里提到 trunk_verdict 是正常的，
        提到就转红会让这条断言变成一个谁都不敢碰注释的地雷。
        """
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        for name in ("trunk_verdict", "fetch_origin"):
            self.assertNotIn(f"def {name}(", src,
                             f"{name} 的定义应该只在 fleet-ops/lib/fleetops_drift.py")
        self.assertIn("from fleetops_drift import trunk_verdict", src)

    def test_addresses_fleet_ops_by_its_stable_entry(self):
        """按生产契约寻址，不从 __file__ 推导（准则 26/27）。

        本检查器有两个跑法——release.sh 从工作树跑、launchd 探针从私有克隆跑。
        从 __file__ 推导的话，两边会去不同的地方找同一份判据。
        """
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        assign = src.split("FLEET_LIB = ", 1)[1].split("\nif str(FLEET_LIB)", 1)[0]
        # 可被环境变量覆盖，只为可测；不覆盖时走生产契约那个默认值。
        self.assertIn('os.environ.get(', assign)
        self.assertIn('"FLEET_OPS_LIB"', assign)
        self.assertIn(".local/share/fleet-ops/current/lib", assign)
        self.assertNotIn("__file__", assign)

    def test_no_fallback_to_a_vendored_copy(self):
        """拿不到就报「查不了」，不许用一份旧的顶上。

        T-0107 刚否掉这个形态：兜底等于把 bug 以兜底之名留下，且只在产物缺失时
        发作——那正是最需要它说真话的时刻。
        """
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        blk = src.split("try:\n    from fleetops_drift", 1)[1].split("\n\n", 1)[0]
        self.assertIn("trunk_verdict = None", blk)
        for fallback in ("def trunk_verdict", "_local_trunk", "vendored"):
            self.assertNotIn(fallback, blk)

    def test_missing_fleet_ops_reports_error_not_ok(self):
        """真跑：判据取不到时落 error + rc=2，绝不是 ok。

        「查不了」和「没问题」混成一句，取数一挂就播报体检通过——这一族今天
        已经在三个地方各修过一次。所以这里跑的是入口 main()，不是读源码。
        """
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            status = pathlib.Path(tmp) / "s.status"
            env = dict(os.environ, FLEET_OPS_LIB="/nonexistent/lib")
            # 带 --ref 是为了让用例不依赖 NAS：判据缺席应当在碰网络之前就返回。
            proc = sp.run([sys.executable, str(ROOT / "deploy" / "check_source_drift.py"),
                           "--ref", "HEAD", "--status-file", str(status)],
                          capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            at, verdict, detail = status.read_text("utf-8").strip().split("\t", 2)
            self.assertEqual(verdict, "error")
            self.assertIn("fleetops_drift", detail)

    def test_missing_fleet_ops_does_not_break_the_listing_modes(self):
        """爆炸半径要关住：prune 不需要主干判决，不该被 fleet-ops 缺席连累。

        这是「在用到的地方判空、不在 import 处抛」那个决定的看守——改成 import
        处抛的话，fleet-ops 一缺席 release.sh 的 prune 步骤会跟着挂。
        """
        import subprocess as sp
        env = dict(os.environ, FLEET_OPS_LIB="/nonexistent/lib")
        proc = sp.run([sys.executable, str(ROOT / "deploy" / "check_source_drift.py"),
                       "--list-prunable", "--ref", "HEAD"],
                      capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_call_site_passes_this_repo(self):
        """共享版按入参判仓库。调用点漏传就会去判 fleet-ops 自己的历史。"""
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        call = next(l for l in src.splitlines()
                    if "trunk_verdict(" in l and "=" in l and "def " not in l)
        self.assertIn("trunk_verdict(REPO, ref)", call)

    def test_listing_modes_do_not_judge_or_write_status(self):
        """列举模式是给 release.sh 的机器可读清单，不是判决。让它出判决的话，
        一次取清单就会覆盖巡检的状态文件。"""
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        self.assertIn("listing = args.list_extra or args.list_prunable", src)
        self.assertIn("if not listing:", src)

    def test_off_trunk_is_drift_and_unknown_is_error(self):
        """三态必须分开：「不在主干上」是真问题（rc=1），「判不了」不是（rc=2）。"""
        src = (ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8")
        blk = src.split("if not listing:", 1)[1].split("try:", 1)[0]
        self.assertIn('write_status(args.status_file, "error", why)', blk)
        self.assertIn('write_status(args.status_file, "drift", why)', blk)
        self.assertIn("return 2", blk)
        self.assertIn("return 1", blk)

    # on/off 分类、单向降级这两条性质的用例现在在
    # fleet-ops/tests/test_fleetops_drift.py（11 例，5 处变异全转红）。
    # 在这边再抄一遍，就是把刚删掉的重复换个地方长回来——而这条收拢治的正是它。

if __name__ == "__main__":
    unittest.main()
