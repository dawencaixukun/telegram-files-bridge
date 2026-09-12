"""测试 /browse 浏览页：SSR 服务端渲染保障、会话列表直出、标题直出与交互接口。"""
import unittest
from fastapi.testclient import TestClient
import preview_server


class TestBrowseFrontend(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(preview_server.app)

    def test_browse_ssr_renders_chats_directly(self):
        """验证会话列表在服务端直出（非 template x-for 客户端空壳），杜绝切页竞态白屏。"""
        resp = self.client.get("/browse")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # 1. 验证会话项已经在 HTML 中直出
        self.assertIn("收藏 (Saved Messages)", html)
        self.assertIn("NodeSeek官方频道", html)
        self.assertIn("FlyClash 交流群", html)
        self.assertIn("chat-item", html)
        self.assertIn('data-chat="8652569586"', html)

        # 2. 验证默认选中首个会话且标题直出
        self.assertIn('id="browseCurTitle"', html)
        self.assertIn("收藏 (Saved Messages)", html)

        # 3. 验证不再依赖 x-data="browsePage()" 客户端渲染会话
        self.assertNotIn('x-data="browsePage()"', html)
        self.assertNotIn('x-for="acc in accounts"', html)

        # 4. 验证顶栏与客户端交互接口齐全
        self.assertIn("__browseSelectChat", html)
        self.assertIn("__browseFilterChats", html)
        self.assertIn("__browseSetType", html)
        self.assertIn("__browseReload", html)

    def test_browse_download_does_not_reset_to_first_page(self):
        """恶性 bug 回归：点「下载」后不得强制 cursor=0 重拉第一页。

        历史行为：__browsePost 成功后调 __browseReload()（内部写死
        cursor=0 并 innerHTML 替换整个网格）→ 用户点一次下载就跳回
        第一页、无限滚动哨兵被换掉，必须一页页重新滚。
        __browseReload 仅保留给切聊天/切类型等「确实需要回第一页」的
        交互；且下载路径不得包含 cursor=0 的重拉调用。"""
        resp = self.client.get("/browse")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("__browseMarkSubmitted", html)
        # __browsePost 成功分支内不得再「调用」__browseReload（注释提及不算）
        post_m = __import__("re").search(
            r"__browsePost = function[\s\S]*?\n  \};", html)
        self.assertIsNotNone(post_m, "__browsePost 函数缺失")
        calls = __import__("re").findall(
            r"(?<!// ).__browseReload\(", post_m.group(0))
        code_calls = [c for c in calls]  # 含注释命中；再排除注释行
        comment_lines = [ln for ln in post_m.group(0).splitlines()
                         if "__browseReload" in ln and ln.strip().startswith("//")]
        self.assertEqual(len(code_calls) - sum(ln.count("__browseReload(") for ln in comment_lines), 0,
                         "下载提交成功后重拉第一页（cursor=0）会跳回顶部+断掉无限滚动")


if __name__ == "__main__":
    unittest.main()
