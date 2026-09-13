# -*- coding: utf-8 -*-
"""
services/doctor_service.py — System Doctor 全链路健康检查与依赖自检探针服务
========================================================================
四大并发探针（Java 后端、TDLib 会话、OpenList 网盘、本地存储水位），2.5s 单项超时隔离，SLA < 3.0s。
"""
import os
import time
import shutil
import asyncio
import httpx
from typing import Any, Dict, List
from core.config import (
    APP_ROOT_DIR, BASE_DIR, _mask_secret, _fmt_size
)
from core.state import (
    _OPENLIST, _is_flood_wait_active
)
from core.backend import BACKEND
from services.openlist_service import (
    _openlist_ready, openlist_dirs
)


async def _doctor_probe_java_backend() -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        data = await asyncio.wait_for(BACKEND.auth_session(), timeout=2.5)
        lat = round((time.perf_counter() - start) * 1000, 1)
        auth = bool(data.get("authenticated")) if isinstance(data, dict) else False
        is_ok = bool(data) and auth
        return {
            "name": "Java 后端核心 (tg-files-api)",
            "status": "healthy" if is_ok else ("warning" if data else "critical"),
            "latencyMs": lat,
            "message": "服务在线，会话认证有效" if is_ok else ("服务在线，但会话未登录" if data else "服务响应异常"),
            "recommendation": "" if is_ok else "请检查 Java Vert.x 核心服务会话凭据",
            "details": {
                "endpoint": "http://backend-service:8123",
                "authenticated": auth,
                "version": "0.4.0",
            }
        }
    except Exception as e:
        lat = round((time.perf_counter() - start) * 1000, 1)
        return {
            "name": "Java 后端核心 (tg-files-api)",
            "status": "critical",
            "latencyMs": lat,
            "message": f"连接失败（{e.__class__.__name__}）",
            "recommendation": "Java 后端不可达，请检查 Docker 或守护进程是否运行在 :8123 端口",
            "details": {
                "endpoint": "http://backend-service:8123",
                "authenticated": False,
            }
        }


async def _doctor_probe_tdlib() -> Dict[str, Any]:
    """TDLib 客户端会话探针（双信号裁决）。

    历史缺陷（用户反馈「TDLib 会话一直异常」）：旧逻辑主探针
    getAuthorizationState 一旦抛非超时异常（后端 502/连接拒绝等 HTTP 层
    失败）就立即 raise 判 critical，哪怕 Telegram 会话实际在跑——
    只要 Java 后端那个端点抖动就一票否决，账号列表兜底形同虚设。

    现在改为双信号：
    1. 主探针 getAuthorizationState → READY 即 healthy；
    2. 传输层/HTTP 层失败（HTTPStatusError/TransportError/超时）不再
       判死，退回 list_telegrams 的 authorized 兜底二次确认；
    3. 两路信号都拿不到授权确认才判 critical，且报错带上 HTTP 状态码。
    TDLib 业务层 error 对象（如 Unauthorized）不属于传输故障，
    仍按登录未完成处理，交由账号列表裁决。
    """
    start = time.perf_counter()
    try:
        is_fw = _is_flood_wait_active()
        is_ready = False
        acc_name = "default"
        acc_count = 1
        td_err_msg = ""

        # 主探针：telegram_api("getAuthorizationState")
        try:
            td_res = await asyncio.wait_for(BACKEND.telegram_api("getAuthorizationState"), timeout=2.5)
            if isinstance(td_res, dict):
                state_type = str(td_res.get("@type") or td_res.get("constructor") or "")
                if state_type in ("authorizationStateReady", "READY", "-1834871737"):
                    is_ready = True
                elif state_type == "error":
                    td_err_msg = str(td_res.get("message") or "TDLib 返回错误")
        except Exception as e:
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                td_err_msg = "TDLib 探针超时（2.5s）"
            elif isinstance(e, (httpx.HTTPStatusError, httpx.TransportError)):
                status = getattr(getattr(e, "response", None), "status_code", None)
                td_err_msg = f"HTTP {status}" if status else e.__class__.__name__
            else:
                # 未知异常维持旧行为：向上抛，走 critical 兜底
                raise

        # 兜底信号：账号列表授权状态
        try:
            telegrams = await asyncio.wait_for(BACKEND.list_telegrams(), timeout=1.0)
            if isinstance(telegrams, list) and telegrams:
                acc_count = len(telegrams)
                for t in telegrams:
                    if isinstance(t, dict) and (t.get("authorized") is True or t.get("lastAuthorizationState") is None):
                        acc_name = str(t.get("phone") or t.get("telegramId") or "default")
                        is_ready = True
                        break
        except Exception:
            pass

        lat = round((time.perf_counter() - start) * 1000, 1)
        if is_fw:
            status = "warning"
            msg = "Telegram 账号处于限流冷却中（风控保护）"
            rec = "风控冷却中，倒计时结束后将自动续跑"
        elif is_ready:
            status = "healthy"
            msg = "Telegram 会话已就绪且已授权"
            rec = ""
        elif td_err_msg:
            # 主探针失败且账号列表也未能确认授权 → 才判 critical
            status = "critical"
            msg = f"TDLib 探针失败（{td_err_msg}），账号列表亦未确认授权"
            rec = "请检查 Telegram 服务端 (8123) 与 TDLib 客户端运行状态"
        else:
            status = "warning"
            msg = "Telegram 登录未完成" if acc_count > 0 else "未绑定任何 Telegram 账号"
            rec = "请前往控制台进行 TG 账号登录向导"
        return {
            "name": "TDLib 客户端会话",
            "status": status,
            "latencyMs": lat,
            "message": msg,
            "recommendation": rec,
            "details": {
                "activeAccount": _mask_secret(acc_name),
                "accountCount": acc_count,
                "isFloodWait": is_fw,
            }
        }

    except Exception as e:
        lat = round((time.perf_counter() - start) * 1000, 1)
        return {
            "name": "TDLib 客户端会话",
            "status": "critical",
            "latencyMs": lat,
            "message": f"TDLib 探针异常（{e.__class__.__name__}）",
            "recommendation": "无法连接 TDLib 会话，请检查 Telegram 服务端运行状态",
            "details": {
                "isFloodWait": _is_flood_wait_active(),
            }
        }




async def _doctor_probe_openlist() -> Dict[str, Any]:
    start = time.perf_counter()
    user_masked = _mask_secret(_OPENLIST.get("username", ""))
    try:
        has_token = bool(_OPENLIST.get("token"))
        verified = await asyncio.wait_for(_openlist_ready(), timeout=2.5) if has_token else False
        mount_count = 0
        if verified:
            dirs_res = await asyncio.wait_for(openlist_dirs("/"), timeout=2.5)
            if isinstance(dirs_res, dict) and dirs_res.get("ok"):
                mount_count = len(dirs_res.get("dirs") or [])
            elif isinstance(dirs_res, list):
                mount_count = len(dirs_res)
        lat = round((time.perf_counter() - start) * 1000, 1)
        status = "healthy" if (verified and mount_count > 0) else ("warning" if verified else "critical")
        msg = f"网盘引擎在线，已挂载 {mount_count} 个云端存储" if (verified and mount_count > 0) else ("网盘服务在线但尚未挂载任何云盘" if verified else "OpenList 未登录或令牌失效")
        rec = "" if (verified and mount_count > 0) else ("请在 OpenList 管理台添加至少一个网盘挂载" if verified else "请在设置页输入 OpenList 凭据重新登录")
        return {
            "name": "OpenList 云端网盘引擎",
            "status": status,
            "latencyMs": lat,
            "message": msg,
            "recommendation": rec,
            "details": {
                "endpoint": "http://openlist-service:5244",
                "tokenValid": verified,
                "username": user_masked or "未配置",
                "mountCount": mount_count,
            }
        }
    except Exception as e:
        lat = round((time.perf_counter() - start) * 1000, 1)
        return {
            "name": "OpenList 云端网盘引擎",
            "status": "critical",
            "latencyMs": lat,
            "message": f"OpenList 探测失败（{e.__class__.__name__}）",
            "recommendation": "请检查 OpenList 服务容器是否在端口 :5244 正常运行",
            "details": {
                "endpoint": "http://openlist-service:5244",
                "tokenValid": False,
                "username": user_masked or "未配置",
            }
        }


async def _doctor_probe_local_storage() -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        test_file = os.path.join(APP_ROOT_DIR, ".doctor_probe.tmp")
        with open(test_file, "wb") as f:
            f.write(b"DOCTOR_OK")
        if os.path.exists(test_file):
            os.remove(test_file)
        usage = shutil.disk_usage(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
        total = usage.total
        free = usage.free
        used = usage.used
        pct = (used / total * 100.0) if total else 0.0
        lat = round((time.perf_counter() - start) * 1000, 1)
        status = "healthy" if pct < 85.0 else ("warning" if pct < 95.0 else "critical")
        msg = f"存储读写正常，磁盘使用率 {pct:.1f}%（{'水位正常' if pct < 85.0 else '触发高水位预警'}）"
        rec = "" if pct < 85.0 else "本地磁盘使用率超 85%，建议及时归档清理本地文件以防下载熔断"
        return {
            "name": "VPS 本地存储与水位",
            "status": status,
            "latencyMs": lat,
            "message": msg,
            "recommendation": rec,
            "details": {
                "writable": True,
                "totalSpace": _fmt_size(total),
                "freeSpace": _fmt_size(free),
                "usedPercent": round(pct, 1),
                "isWatermarkMeltdown": pct >= 85.0,
            }
        }
    except Exception as e:
        lat = round((time.perf_counter() - start) * 1000, 1)
        return {
            "name": "VPS 本地存储与水位",
            "status": "critical",
            "latencyMs": lat,
            "message": f"本地存储探测异常（{e.__class__.__name__}）",
            "recommendation": "本地存储目录无写入权限或磁盘已满，请检查目录权限与挂载状态",
            "details": {
                "writable": False,
                "isWatermarkMeltdown": True,
            }
        }


async def _run_system_doctor_check() -> Dict[str, Any]:
    start_total = time.perf_counter()
    probes = [
        _doctor_probe_java_backend(),
        _doctor_probe_tdlib(),
        _doctor_probe_openlist(),
        _doctor_probe_local_storage(),
    ]
    results = await asyncio.gather(*probes, return_exceptions=True)
    res_list = []
    for r in results:
        if isinstance(r, Exception):
            res_list.append({
                "name": "未识别组件",
                "status": "critical",
                "latencyMs": 0.0,
                "message": f"探针执行异常（{r.__class__.__name__}）",
                "recommendation": "请查看系统日志",
                "details": {}
            })
        else:
            res_list.append(r)

    java_b, tdlib_b, openlist_b, storage_b = res_list
    comps = {
        "javaBackend": java_b,
        "tdlib": tdlib_b,
        "openlist": openlist_b,
        "localStorage": storage_b,
    }

    statuses = [c.get("status") for c in comps.values()]
    if "critical" in statuses:
        overall = "critical"
        summary_msg = "系统发现关键依赖组件故障，部分转存服务可能不可用"
    elif "warning" in statuses:
        overall = "warning"
        summary_msg = "系统整体运行中，但存在预警项（如限流冷却或高磁盘水位）"
    else:
        overall = "healthy"
        summary_msg = "所有核心服务与依赖探针均运行良好，全链路健康"

    recs = [c["recommendation"] for c in comps.values() if c.get("recommendation")]
    total_lat = round((time.perf_counter() - start_total) * 1000, 1)

    return {
        "ok": True,
        "timestamp": time.time(),
        "overallStatus": overall,
        "summaryMessage": summary_msg,
        "totalLatencyMs": total_lat,
        "components": comps,
        "recommendations": recs,
    }
