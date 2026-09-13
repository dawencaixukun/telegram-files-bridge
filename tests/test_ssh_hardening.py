# -*- coding: utf-8 -*-
"""
test_ssh_hardening.py — VPS 部署通道 SSH 私钥认证与免密运维自动化测试套件
========================================================================
测试目标与覆盖矩阵：
1. 凭据存储安全性规范：
   - 彻底剥离明文密码：核验 .vps-conn/vps.json 严禁包含 password / passwd 字段
   - 私钥路径配置规范：配置 privateKeyPath 并指向合规 SSH 密钥
2. 部署与远程执行脚本鉴权加固 (deploy.js & exec.js):
   - 强制使用本地 ED25519/RSA 私钥认证
   - 严禁任何明文密码 fallback 或回退通道
3. SSH 私钥免密远程执行实测验证:
   - 执行 node .vps-conn/exec.js "echo SSH_KEY_OK"，验证 100% 凭私钥秒级连通 VPS 并输出 SSH_KEY_OK
4. SSH 部署与热重启流水线验证 (deploy.js):
   - 执行 node .vps-conn/deploy.js 验证基于 SSH/SFTP 私钥通道成功完成文件比对、增量上传与服务热重启
"""
import json
import os
import subprocess
import unittest


class TestSshHardening(unittest.TestCase):
    def setUp(self):
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = os.path.dirname(cur_dir) if os.path.basename(cur_dir) == "tests" else cur_dir
        self.vps_conn_dir = os.path.join(self.base_dir, ".vps-conn")
        self.vps_json_path = os.path.join(self.vps_conn_dir, "vps.json")
        self.deploy_js_path = os.path.join(self.vps_conn_dir, "deploy.js")
        self.exec_js_path = os.path.join(self.vps_conn_dir, "exec.js")


    def test_vps_json_no_plaintext_password(self):
        """【安全门禁】：验证 vps.json 彻底剥离明文密码，只保留主机、端口与私钥路径"""
        self.assertTrue(os.path.exists(self.vps_json_path), "vps.json 必须存在")
        with open(self.vps_json_path, "r", encoding="utf-8") as f:
            conf = json.load(f)

        # 核心安全红线：严禁出现 password 字段
        self.assertNotIn("password", conf, "vps.json 绝对不能包含明文 password 字段！")
        self.assertNotIn("passwd", conf, "vps.json 绝对不能包含明文 passwd 字段！")

        # 必须配置私钥路径或使用密钥
        has_key = "privateKeyPath" in conf or "private_key_path" in conf
        self.assertTrue(has_key, "vps.json 必须配置 privateKeyPath")
        self.assertEqual(conf.get("user"), "root")

    def test_deploy_and_exec_scripts_enforce_private_key_only(self):
        """验证 deploy.js 与 exec.js 源码中强制仅凭私钥连接，无密码 fallback"""
        for script_path in (self.deploy_js_path, self.exec_js_path):
            self.assertTrue(os.path.exists(script_path))
            with open(script_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("resolvePrivateKey", content)
            self.assertIn("privateKey:", content)
            # 确认没有 password: conf.password
            self.assertNotIn("password: conf.password", content)
            self.assertNotIn("password: conf.pass", content)
            self.assertIn("已剥离明文密码，拒绝连接", content)

    def test_ssh_exec_with_private_key_success(self):
        """【核心验收条件 3 部分】：验证仅凭本地 SSH 私钥免密执行远程命令成功输出 SSH_KEY_OK"""
        res = subprocess.run(
            ["node", self.exec_js_path, "echo SSH_KEY_OK"],
            capture_output=True,
            text=True,
            cwd=self.base_dir,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"SSH 私钥命令执行失败: {res.stderr}")
        self.assertIn("SSH_KEY_OK", res.stdout.strip())

    def test_ssh_deploy_with_private_key_success(self):
        """【核心验收条件 3】：验证 node .vps-conn/deploy.js 纯基于 SSH 密钥成功连接 VPS 完成部署验证"""
        res = subprocess.run(
            ["node", self.deploy_js_path],
            capture_output=True,
            text=True,
            cwd=self.base_dir,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, f"SSH 部署执行失败: {res.stderr}")
        stdout = res.stdout
        self.assertIn("ALL UPLOADED", stdout, "部署输出应包含 ALL UPLOADED")
        self.assertIn("tg-bridge RESTARTED SUCCESSFULLY", stdout, "部署输出应包含服务重启成功")


if __name__ == "__main__":
    unittest.main()
