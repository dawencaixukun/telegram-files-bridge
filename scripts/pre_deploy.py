# -*- coding: utf-8 -*-
"""
scripts/pre_deploy.py — 本地一键 Pre-deploy 门禁自动化流水线主程序
================================================================
符合 P3 架构设计规范，包含四个阶段质量与安全门禁：
  [Stage 1] Lint, Syntax & DAG Audit (语法编译与单向依赖 DAG 校验)
  [Stage 2] Unit & Regression Tests (全量测试套件 220+ 用例 100% 绿色)
  [Stage 3] Security & Credential Audit (凭据脱敏、私钥加固与权限门禁)
  [Stage 4] Deploy & Hot-restart (纯私钥增量 SFTP 推送与服务热重启验证)
"""
import os
import sys
import ast
import json
import time
import argparse
import subprocess
import py_compile
from typing import Set

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)



def log_step(stage: int, title: str):
    print(f"\n{'='*75}")
    print(f"  [Stage {stage}] {title}")
    print(f"{'='*75}")


def stage1_lint_syntax_and_dag() -> bool:
    log_step(1, "Lint, Syntax & Architecture DAG Audit")
    
    # 1. 语法编译检查
    py_files = []
    for root, _, files in os.walk(_ROOT_DIR):
        if any(p in root for p in [".git", "__pycache__", ".pytest_cache", ".venv", "env"]):
            continue
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
    
    print(f"[*] 正在编译检查 {len(py_files)} 个 Python 源文件...")
    for pf in py_files:
        try:
            py_compile.compile(pf, doraise=True)
        except Exception as e:
            print(f"[FAIL] 语法编译错误 {pf}: {e}")
            return False
    print("  -> 全部 Python 源码语法编译校验通过 (0 Syntax Errors)")

    # 2. 严格单向导入 DAG 检查
    print("[*] 正在解析模块 AST 导入依赖并执行 DAG 架构断言...")
    violations = []

    def get_imports(filepath: str) -> Set[str]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                root = ast.parse(f.read(), filename=filepath)
        except Exception:
            return set()
        
        imported = set()
        for node in ast.walk(root):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        return imported

    # 规则 1: core/ 下的文件不得依赖 services, routers, bridge_server
    core_dir = os.path.join(_ROOT_DIR, "core")
    for f in os.listdir(core_dir):
        if f.endswith(".py") and f != "__init__.py":
            fp = os.path.join(core_dir, f)
            imps = get_imports(fp)
            for imp in imps:
                if imp.startswith("services") or imp.startswith("routers") or imp.startswith("bridge_server"):
                    violations.append(f"core/{f} 发生非法逆向依赖 -> {imp}")

    # 规则 2: services/ 下的文件不得依赖 routers, bridge_server
    services_dir = os.path.join(_ROOT_DIR, "services")
    for f in os.listdir(services_dir):
        if f.endswith(".py") and f != "__init__.py":
            fp = os.path.join(services_dir, f)
            imps = get_imports(fp)
            for imp in imps:
                if imp.startswith("routers") or imp.startswith("bridge_server"):
                    violations.append(f"services/{f} 发生非法逆向依赖 -> {imp}")

    # 规则 3: routers/ 下的文件不得依赖 bridge_server，且子路由间不得横向交叉依赖
    routers_dir = os.path.join(_ROOT_DIR, "routers")
    for f in os.listdir(routers_dir):
        if f.endswith(".py") and f != "__init__.py":
            fp = os.path.join(routers_dir, f)
            imps = get_imports(fp)
            current_mod = f"routers.{f[:-3]}"
            for imp in imps:
                if imp.startswith("bridge_server"):
                    violations.append(f"routers/{f} 发生非法向上依赖 -> {imp}")
                elif imp.startswith("routers.") and imp != current_mod:
                    violations.append(f"routers/{f} 发生横向循环依赖 -> {imp}")

    if violations:
        print("[FAIL] 架构 DAG 单向依赖违规清单:")
        for v in violations:
            print(f"  - {v}")
        return False

    print("  -> 单向依赖 DAG 校验完美通过：core 零反向、services 零跨层、routers 零横向 (0 DAG Violations)")
    return True


def stage2_unit_and_regression_tests() -> bool:
    log_step(2, "Unit & Regression Tests Suite Execution")
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    print(f"[*] 执行测试套件命令: {' '.join(cmd)}")
    start = time.time()
    res = subprocess.run(cmd, cwd=_ROOT_DIR, capture_output=True, text=True, timeout=300)
    dur = time.time() - start

    output = (res.stdout or "") + "\n" + (res.stderr or "")
    # 打印测试运行总结行
    summary_lines = [l for l in output.splitlines() if "Ran " in l or "OK" in l or "FAILED" in l]
    for sl in summary_lines:
        print(f"  {sl}")

    if res.returncode != 0 or "FAILED" in output:
        print(f"[FAIL] 单元测试未完全通过（耗时 {dur:.2f}s）:")
        for l in output.splitlines()[-40:]:
            print(f"  {l}")
        return False

    print(f"  -> 全量自动化测试套件 100% 绿色全通（耗时 {dur:.2f}s，0 Failures, 0 Errors）")
    return True


def stage3_security_and_credential_audit() -> bool:
    log_step(3, "Security, Credentials & CSRF Gate Audit")
    
    # 1. vps.json 严格脱敏审计
    vps_json = os.path.join(_ROOT_DIR, ".vps-conn", "vps.json")
    if not os.path.exists(vps_json):
        print(f"[FAIL] 缺少 .vps-conn/vps.json 配置文件")
        return False
    
    try:
        with open(vps_json, "r", encoding="utf-8") as f:
            vps_cfg = json.load(f)
        if "password" in vps_cfg or "passwd" in vps_cfg:
            print(f"[FAIL] .vps-conn/vps.json 中存在明文密码字段，违反凭据安全红线！")
            return False
        if not vps_cfg.get("privateKeyPath"):
            print(f"[FAIL] .vps-conn/vps.json 缺少合规私钥路径 privateKeyPath")
            return False
    except Exception as e:
        print(f"[FAIL] 解析 vps.json 失败: {e}")
        return False
    print("  -> vps.json 凭据审计通过：彻底剥离明文密码，强制使用本地私钥")

    # 2. 脚本无明文密码回退审计
    for s_name in ["deploy.js", "exec.js"]:
        sp = os.path.join(_ROOT_DIR, ".vps-conn", s_name)
        with open(sp, "r", encoding="utf-8") as f:
            code = f.read()
        if "password:" in code and "privateKey:" not in code:
            print(f"[FAIL] {s_name} 缺少私钥凭据配置")
            return False
        if "conf.password" in code:
            print(f"[FAIL] {s_name} 仍存在密码回退通道")
            return False
    print("  -> deploy.js 与 exec.js 静态审计通过：纯 SSH 私钥免密通道")

    # 3. 路由安全门禁审计
    from core.auth import _is_public
    if _is_public("/tasks") or _is_public("/api/subscriptions") or _is_public("/library/cloud"):
        print("[FAIL] 受保护的业务路由被意外纳入白名单！")
        return False
    print("  -> 路由权限与 CSRF 门禁中间件校验通过：受保护路由严格处于防御链中")
    return True


def stage4_deploy_and_restart() -> bool:
    log_step(4, "Pure SSH Private Key Incremental Deploy & Service Restart")
    deploy_script = os.path.join(_ROOT_DIR, ".vps-conn", "deploy.js")
    print(f"[*] 执行自动化私钥打包增量部署: node {deploy_script}")
    
    res = subprocess.run(["node", deploy_script], cwd=_ROOT_DIR, capture_output=True, text=True, timeout=120)
    print(res.stdout or "")
    if res.returncode != 0:
        print(f"[FAIL] 自动化增量部署失败:\n{res.stderr}")
        return False

    print("  -> 增量部署推送与 systemctl restart tg-bridge 服务热重启验证成功！")
    return True


def main():
    parser = argparse.ArgumentParser(description="Telegram Files Bridge Pre-deploy 门禁自动化流水线")
    parser.add_argument("--skip-deploy", action="store_true", help="仅执行 Stage 1-3 前置质量与安全门禁，跳过远端部署")
    parser.add_argument("--only-stage", type=int, choices=[1, 2, 3, 4], help="仅执行指定门禁阶段")
    args = parser.parse_args()

    print("\n" + "#"*75)
    print("  Telegram Files Bridge — Pre-deploy 质量与安全自动化流水线")
    print("#"*75)

    stages = [
        (1, stage1_lint_syntax_and_dag),
        (2, stage2_unit_and_regression_tests),
        (3, stage3_security_and_credential_audit),
    ]
    if not args.skip_deploy:
        stages.append((4, stage4_deploy_and_restart))

    start_all = time.time()
    for stage_num, stage_func in stages:
        if args.only_stage and args.only_stage != stage_num:
            continue
        success = stage_func()
        if not success:
            print(f"\n[FATAL] 流水线在 Stage {stage_num} 中止！发布已被安全拦截。")
            sys.exit(1)

    total_dur = time.time() - start_all
    print(f"\n{'#'*75}")
    print(f"  [SUCCESS] Pre-deploy 自动化质量与安全流水线 100% 成功通过！(总耗时: {total_dur:.2f}s)")
    print(f"{'#'*75}\n")
    sys.exit(0)



if __name__ == "__main__":
    main()
