"""report-portal 的部署准则机器检查。

准则文档会被绕过，因为它不参与任何判断；带机器检查的准则才有效。这个文件把
deploy-standard 里适用于本服务的几条，变成会当场变红的断言。

每个用例的 docstring 写它防的是哪一条、以及那条是怎么被踩出来的。
"""
import hashlib
import io
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
        # 真正的投放这一步（--format=tar 那条，区别于 dry-run 里只用来数条目的）
        publish = [l for l in RELEASE.splitlines() if "archive --format=tar" in l]
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
        # 删这一步不能拿全量 extras
        prune_block = RELEASE.split("[5.5]", 1)[1].split("[6/7]", 1)[0]
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
        self.assertIn('EXTRA_RC=$?', RELEASE)                  # 真取了退出码
        # 判据必须是「非 0」，不能是「等于某个预料中的码」。第一版写成 `= "2"`，
        # 只堵住了检测自己主动 return 2 的那种失败；检查器崩掉（未捕获异常）给的
        # 是 rc=1，$EXTRA 同样为空，于是直接落进「没有多余文件」——正是本用例要
        # 防的那句话。实测 python 未捕获异常退出码就是 1。
        self.assertIn('[ "$EXTRA_RC" != "0" ]', RELEASE)
        # 而且这个判断必须排在「没有多余文件」那句之前，否则查不了会先被归成「干净」
        guard = RELEASE.index('[ "$EXTRA_RC" != "0" ]')
        empty = RELEASE.index("没有多余文件")
        self.assertLess(guard, empty)

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
        import os
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                anc = mod.historical_hashes("f.txt", "main")
                everywhere = mod.historical_hashes("f.txt", None)
            finally:
                os.chdir(cwd)
        self.assertIn(sha["A"], anc)
        self.assertIn(sha["C"], anc)
        self.assertNotIn(sha["B"], anc)          # 侧分支的内容不在 main 的祖先链上
        self.assertIn(sha["B"], everywhere)      # 但 --all 看得见它（只用于分类）

    def test_list_prunable_uses_the_release_ref_not_all(self):
        """光证明函数对没用——还得证明**有人在用它，并且传对了 ref**。
        （kg-hub-edit 的变异验证里，「把判据退回 was_ever_tracked」没转红，就是因为
        用例只验了函数本身、从没跑过调用点。）这里真跑 main()。"""
        import os
        from unittest import mock
        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sha = self._temp_repo(tmp)
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with mock.patch.object(mod, "git_side", lambda ref: {}), \
                     mock.patch.object(mod, "nas_side", lambda: {"f.txt": sha["B"]}), \
                     mock.patch.object(sys, "argv",
                                       ["x", "--list-prunable", "--ref", "main"]), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                     mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    rc = mod.main()
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 0)
        # 侧分支的内容不该进可删清单……
        self.assertEqual(out.getvalue().strip(), "")
        # ……而且要说清它为什么被跳过，措辞不能写成「有人改了生产」
        self.assertIn("不在 main 这条线上", err.getvalue())

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
        loop = block.split("for p in only_nas:", 1)[1]
        gate = next(l.strip() for l in loop.splitlines() if l.strip().startswith("if "))
        self.assertIn("historical_hashes", gate)      # 放行看的是内容能否在历史里找到
        self.assertNotIn("was_ever_tracked", gate)    # 「曾被跟踪」只配解释为什么跳过


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


if __name__ == "__main__":
    unittest.main()
