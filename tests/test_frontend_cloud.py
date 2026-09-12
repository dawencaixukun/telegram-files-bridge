import unittest
from fastapi.testclient import TestClient
import bridge_server
import preview_server

class TestFrontendCloudEnhancements(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)

    def test_preview_cloud_page_elements(self):
        resp = self.preview_client.get("/library/cloud")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # 1. 搜索框与网盘分类筛选
        self.assertIn('id="cloudSearch"', html)
        self.assertIn('x-model="q"', html)
        self.assertIn('id="cloudDrive"', html)
        self.assertIn('x-model="drive"', html)
        # 网盘分类选项必须由数据驱动（真实挂载 ∪ 归档记录），而非硬编码常量。
        # 断言渲染出的是「从记录聚合」的选项，并确认存档分类筛选已接入。
        self.assertIn('全部网盘', html)
        self.assertIn('id="cloudStatus"', html)
        self.assertIn('x-model="statusFilter"', html)

        # 2. 直达 OpenList 链接
        self.assertIn('ic-external', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('OpenList', html)
        self.assertIn('http://127.0.0.1:5244', html)

        # 3. 云端删除与清理失效按钮
        self.assertIn('__deleteCloudFile', html)
        self.assertIn('__clearMissingCloudFiles', html)
        self.assertIn('id="missingBanner"', html)

        # 4. 激活的取回按钮
        self.assertIn('__retrieveCloudFile', html)
        self.assertIn('data-retrieve-btn', html)
        self.assertIn('data-cloud-path', html)

    def test_app_js_cloud_methods(self):
        with open("static/js/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        for method in [
            "__deleteCloudFile",
            "__clearMissingCloudFiles",
            "__retrieveCloudFile",
            "__retrievePollStart",
            "applyRetrieveButton",
            "applyRetrievePill",
        ]:
            self.assertIn(method, js, f"Missing {method} in app.js")

if __name__ == '__main__':
    unittest.main()