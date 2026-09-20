"""/health 的行为测试——真起 app、真发请求，不是断言源码里有哪句话。

这个端点的全部价值在于「坏的时候会红」。所以这里每一条都先把某样东西弄坏，
再看它是不是真的红了；只验绿的那一半等于没验（准则 25）。
"""
import unittest
from unittest import mock

from starlette.testclient import TestClient

import portal


def _src(sid, ok=True, cards=1):
    return {"id": sid, "name": sid, "ok": ok,
            "cards": [{"name": f"{sid}-card", "desc": "", "icon": "📄",
                       "ready": True, "url": "http://x/y"} for _ in range(cards)],
            **({} if ok else {"error": "ConnectError"})}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(portal.app)

    def _get(self, gather_result=None, gather_exc=None):
        async def fake_gather():
            if gather_exc:
                raise gather_exc
            return gather_result
        with mock.patch.object(portal, "_gather", fake_gather):
            return self.client.get("/health")

    def test_reports_ok_with_the_real_card_count(self):
        r = self._get([_src("a", cards=2), _src("b", cards=1)])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["cards"], 3)          # 真数出来的，不是配置里的条数
        self.assertEqual(body["sources"], {"total": 2, "ok": 2, "failed": []})
        self.assertGreater(body["rendered_bytes"], 0)

    def test_an_unreachable_source_is_degraded_but_still_200(self):
        """门户的活是聚合与导航。别人家面板停机是它如实上报的数据，不是它自己的
        故障——这里要是 503，release.sh 会因为别人停机把门户自动回滚掉。"""
        r = self._get([_src("a"), _src("down-one", ok=False, cards=0)])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["sources"]["failed"], ["down-one"])
        self.assertEqual(body["cards"], 1)

    def test_no_cards_at_all_is_down(self):
        """页面还是 200、结构完整、一张卡都没有——活着但不干活。"""
        r = self._get([_src("a", ok=False, cards=0)])
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "down")

    def test_a_broken_aggregation_is_down_not_ok(self):
        r = self._get(gather_exc=RuntimeError("boom"))
        self.assertEqual(r.status_code, 503)
        self.assertIn("boom", r.json()["error"])

    def test_a_template_that_silently_drops_the_data_is_down(self):
        """最要命的一种：模板里把 __DATA__ 改名，`.replace` 静默无操作，页面照常
        200 而一张卡都渲染不出来。字符串替换不报错，所以只能自己查。"""
        with mock.patch.object(portal, "_PORTAL_HTML", "<html>占位符被改名了</html>"):
            r = self._get([_src("a")])
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "down")

    def test_the_page_itself_refuses_to_serve_a_dataless_render(self):
        """同一个故障下，页面不能装作没事——否则 health 红了而用户看到的是
        一个空白但 200 的门户。"""
        with mock.patch.object(portal, "_PORTAL_HTML", "<html>占位符被改名了</html>"):
            with self.assertRaises(RuntimeError):
                portal.render_portal([_src("a")])

    def test_health_renders_through_the_same_path_as_the_page(self):
        """准则 28：判断的两端必须同源。health 要是自己拼一份 HTML，它验证的就
        不是用户实际拿到的东西。"""
        data = [_src("a")]
        seen = []
        real = portal.render_portal

        def spy(d):
            seen.append(d)
            return real(d)

        with mock.patch.object(portal, "render_portal", spy):
            self.assertEqual(self._get(data).status_code, 200)
        self.assertEqual(seen, [data])


if __name__ == "__main__":
    unittest.main()
