import unittest
from fastapi.testclient import TestClient
import bridge_server
import preview_server

class TestFrontendLocalEnhancements(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)
        token = bridge_server._make_portal_token()
        self.cookies = {bridge_server.PORTAL_COOKIE: token}

    def test_library_local_page_elements(self):
        # 1. Preview server /library/local
        resp = self.preview_client.get("/library/local")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn('id="localBatchBar"', html)
        self.assertIn('id="localSelectedCount"', html)
        self.assertIn('id="btnBatchArchive"', html)
        self.assertIn('id="btnBatchDelete"', html)
        self.assertIn('floating-batch-bar', html)
        self.assertIn('card-check', html)

        # 2. Partials /partials/local-files
        resp_p = self.preview_client.get("/partials/local-files")
        self.assertEqual(resp_p.status_code, 200)
        p_html = resp_p.text
        self.assertIn('local-check', p_html)
        self.assertIn('id="localSelAllTop"', p_html)
        self.assertIn('id="localSelAllTable"', p_html)
        self.assertIn('__deleteLocalOne', p_html)
        self.assertIn('__localCardClick', p_html)
        self.assertIn('__localCheckChange', p_html)

    def test_archive_modal_delete_local_checkbox(self):
        resp = self.preview_client.get("/library/local")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn('id="archDeleteLocal"', html)
        self.assertIn('归档成功后自动删除本地原文件', html)

    def test_app_js_local_methods(self):
        with open("static/js/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        for method in [
            "__localCheckChange",
            "__localUpdateSelection",
            "__localSelAll",
            "__localClearSelection",
            "__localCardClick",
            "__bulkArchiveLocal",
            "__deleteLocalOne",
            "__bulkDeleteLocal",
            "archDeleteLocal",
            "deleteLocal",
        ]:
            self.assertIn(method, js, f"Missing {method} in app.js")

if __name__ == '__main__':
    unittest.main()