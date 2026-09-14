# -*- coding: utf-8 -*-
"""
services/backup_service.py — TG Session 异地加密冷备与秒级解密还原引擎
======================================================================
负责扫描 TDLib 会话核心凭据、tar+AES-256-GCM AEAD 加密、推送到 OpenList /TG-Backups/ 并支持灾难恢复还原。
"""
import os
import io
import time
import json
import secrets
import hashlib
import tarfile
import asyncio
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple, Union
import re
import shutil

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

from core.config import (
    APP_ROOT_DIR, _SESSION_BACKUP_REMOTE_DIR, _SESSION_BACKUP_MAX_KEEP,
    _SESSION_BACKUP_CONTAINER, _SESSION_BACKUP_PRERESTORE, _archive_norm_dir
)
from core.state import (
    _ARCHIVE_CONFIG, _archive_config_save
)
from core.auth import _portal_secret
from core.logging import log, LOG_STORE
from services.openlist_service import (
    _openlist_client, _openlist_upload_client, _openlist_ready,
    _openlist_token, _openlist_relogin, _openlist_env,
    _openlist_mkdir_tree, _openlist_exists,
    openlist_list_files, openlist_download_to_file
)

_SESSION_BACKUP_MAGIC = b"TGSNAP01"


def _get_session_backup_secret() -> bytes:
    """获取会话备份主密钥材料（优先环境变量，次选受保护密钥文件，末选 portal 密钥）"""
    env_key = os.environ.get("TG_SESSION_BACKUP_KEY")
    if env_key:
        return env_key.encode("utf-8")
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        key_file = _session_backup_key_file()
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                data = f.read()
            if len(data) >= 16:
                return data
        data = secrets.token_bytes(32)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("读取/生成 session_backup_key 异常，回退使用 portal 密钥: %s", e)
        return _portal_secret()


def _scan_session_files(base_dir: str = APP_ROOT_DIR) -> List[Tuple[str, str]]:
    """白名单扫描核心会话文件与系统凭据，严格排除视频/图片等动辄数 GB 的多媒体缓存"""
    included: List[Tuple[str, str]] = []
    if not os.path.exists(base_dir):
        return included

    acct_dir = os.path.join(base_dir, "account")
    if os.path.isdir(acct_dir):
        try:
            for entry in os.listdir(acct_dir):
                sub = os.path.join(acct_dir, entry)
                if os.path.isdir(sub):
                    # 只收 td.binlog（TDLib 授权密钥本体），
                    # 刻意排除 account/*/db.sqlite*（Chat/消息缓存，实测 502 MB，占备份 97%）：
                    # 该库由 TDLib 从 TG 服务器自动重建，冗余备份没有意义。
                    binlog = os.path.join(sub, "td.binlog")
                    if os.path.isfile(binlog):
                        included.append((binlog, f"account/{entry}/td.binlog"))
        except Exception as e:
            log.warning("扫描 account 目录失败: %s", e)

    config_names = [
        ".backend_creds",
        ".bridge_secret",
        ".openlist_auth",
        ".subscriptions.json",
        ".archive_jobs.json",
        ".archive_config.json",
        ".notify_config.json",
        ".session_backup_key",
        # 后端（telegram-files Java 服务）自己的库，内含 admin_account 表
        # （管理台登录账号 + 密码哈希）。体积仅 ~1 MB，但丢了就登不进管理台。
        "data.db",
        "data.db-wal",
        "data.db-shm",
    ]
    for cf in config_names:
        p = os.path.join(base_dir, cf)
        if os.path.isfile(p):
            included.append((p, cf))

    return included


def _create_session_archive_bytes(base_dir: str = APP_ROOT_DIR) -> Tuple[bytes, List[str]]:
    files = _scan_session_files(base_dir)
    tar_buf = io.BytesIO()
    rel_names: List[str] = []
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        for abs_path, rel_path in files:
            try:
                tar.add(abs_path, arcname=rel_path)
                rel_names.append(rel_path)
            except Exception as e:
                log.warning("打包会话文件失败 %s: %s", abs_path, e)

        manifest_data = json.dumps({
            "backup_time": time.time(),
            "files": rel_names,
            "base_dir": base_dir
        }, indent=2).encode("utf-8")
        ti = tarfile.TarInfo(name="session_backup_manifest.json")
        ti.size = len(manifest_data)
        ti.mtime = int(time.time())
        ti.mode = 0o600
        tar.addfile(ti, io.BytesIO(manifest_data))
        rel_names.append("session_backup_manifest.json")

    return tar_buf.getvalue(), rel_names


def _encrypt_session_payload(plain_bytes: bytes, master_key: Optional[bytes] = None) -> bytes:
    if AESGCM is None:
        raise RuntimeError("缺少 cryptography 依赖，无法执行 AES-256-GCM 加密")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key_material = master_key or _get_session_backup_secret()
    aes_key = hashlib.pbkdf2_hmac("sha256", key_material, salt, 100000, 32)
    aesgcm = AESGCM(aes_key)
    ct = aesgcm.encrypt(nonce, plain_bytes, _SESSION_BACKUP_MAGIC)
    return _SESSION_BACKUP_MAGIC + salt + nonce + ct


def _decrypt_session_payload(enc_bytes: bytes, master_key: Optional[bytes] = None) -> bytes:
    if AESGCM is None:
        raise RuntimeError("缺少 cryptography 依赖，无法执行 AES-256-GCM 解密")
    if len(enc_bytes) < 8 + 16 + 12 + 16:
        raise ValueError("备份数据长度不合法，小于最小包头长度")
    if enc_bytes[:8] != _SESSION_BACKUP_MAGIC:
        raise ValueError(f"非法的备份文件标识符: {enc_bytes[:8]!r}，预期 {_SESSION_BACKUP_MAGIC!r}")
    salt = enc_bytes[8:24]
    nonce = enc_bytes[24:36]
    ct = enc_bytes[36:]
    key_material = master_key or _get_session_backup_secret()
    aes_key = hashlib.pbkdf2_hmac("sha256", key_material, salt, 100000, 32)
    aesgcm = AESGCM(aes_key)
    try:
        return aesgcm.decrypt(nonce, ct, _SESSION_BACKUP_MAGIC)
    except Exception as e:
        raise ValueError("会话备份包校验失败：密文被篡改或解密密钥不匹配") from e


_SESSION_BACKUP_MAX_TOTAL_BYTES = 8 * 1024 ** 3  # 本地冷备目录总积上限 8GB


def _is_local_backup_name(f: str) -> bool:
    """识别本目录内的会话备份包（两种命名都要认，否则会漏清理导致磁盘堆积）。

    - bridge 自身加密冷备：`tg-session-<ts>.tar.gz.enc`（单数 session）
    - backup-sessions.sh 明文快照：`tg-sessions-<ts>.tar.gz`（复数 sessions）
    历史 bug 只匹配前者，导致 shell 脚本产出的包从不被轮转，实测堆积到 16GB+。
    """
    if not (f.startswith("tg-session-") or f.startswith("tg-sessions-")):
        return False
    return f.endswith(".tar.gz") or f.endswith(".tar.gz.enc")


def _rotate_local_backups(dest_dir: str, keep: int = _SESSION_BACKUP_MAX_KEEP) -> None:
    try:
        if not os.path.exists(dest_dir):
            return
        files = []
        for f in os.listdir(dest_dir):
            if _is_local_backup_name(f):
                full = os.path.join(dest_dir, f)
                if os.path.isfile(full):
                    try:
                        files.append((os.path.getmtime(full), os.path.getsize(full), full))
                    except OSError:
                        continue
        files.sort(key=lambda x: x[0], reverse=True)

        # 1) 按份数保留最新 keep 份
        doomed = list(files[keep:])

        # 2) 总积兜底：即便份数未超，累计超出上限也要从最旧的开始删，
        #    防止个别超大包（例如内含 TDLib 视频分片缓存）撑爆磁盘。
        survivors = files[:keep]
        total = sum(sz for _, sz, _ in survivors)
        for item in reversed(survivors):
            if total <= _SESSION_BACKUP_MAX_TOTAL_BYTES:
                break
            total -= item[1]
            doomed.append(item)

        for _, sz, path in doomed:
            try:
                os.remove(path)
                log.info("已轮转清理旧本地冷备: %s (%.2f GB)", path, sz / (1 << 30))
            except Exception:
                pass
    except Exception as e:
        log.warning("本地冷备轮转异常: %s", e)


async def _openlist_mount_names(token: str) -> List[str]:
    """列出根目录下的网盘挂载名（顺序即 OpenList 返回顺序，调用方不得依赖其稳定性）。"""
    try:
        resp = await _openlist_client.post(
            "/api/fs/list",
            json={"path": "/", "password": "", "page": 1, "per_page": 100, "refresh": False},
            headers={"Authorization": token})
        code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, dict):
            return [str(i["name"]) for i in (data.get("content") or [])
                    if isinstance(i, dict) and i.get("is_dir") and i.get("name")]
    except Exception as e:  # noqa: BLE001
        log.debug("列出 OpenList 网盘挂载失败: %s", e)
    return []


async def _mount_has_backups(token: str, mount: str) -> bool:
    """该网盘下是否已存在 TG-Backups 目录（用于跨重启稳定复用，而非猜第一个）。"""
    try:
        resp = await _openlist_client.post(
            "/api/fs/list",
            json={"path": f"/{mount}", "password": "", "page": 1, "per_page": 200, "refresh": False},
            headers={"Authorization": token})
        code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, dict):
            return any(isinstance(i, dict) and i.get("name") == "TG-Backups"
                       for i in (data.get("content") or []))
    except Exception as e:  # noqa: BLE001
        log.debug("探测网盘 %s 是否已有备份目录失败: %s", mount, e)
    return False


async def _resolve_session_backup_remote_dir_detail(token: Optional[str] = None) -> Tuple[str, str, List[str]]:
    """解析云端冷备目录，返回 (目录, 来源说明, 网盘列表)。

    优先级（核心约束：绝不静默把备份写到用户没指定的网盘）：
      1. 环境变量 TG_SESSION_BACKUP_REMOTE_DIR
      2. 设置页显式指定的自定义路径（sessionBackupDir）
      3. 复用任一网盘中**已存在**的 TG-Backups 目录（跨重启稳定）
      4. 跟随「归档目录」所在网盘 → /<网盘>/TG-Backups
      5. 只有一个网盘时自动选择
      6. 多网盘且无从判断 → 返回空串，由调用方提示用户显式指定

    历史 bug：第 6 种情况曾退化为「取根目录下第一个目录」，
    于是 /google、/onedrive、/ppan... 的排序一变，备份就会悄悄换到别的网盘，
    且旧备份在下拉里消失、看起来像“备份丢了”。
    """
    env_dir = os.environ.get("TG_SESSION_BACKUP_REMOTE_DIR")
    if env_dir:
        return env_dir.rstrip("/"), "环境变量 TG_SESSION_BACKUP_REMOTE_DIR", []

    cfg_dir = str(_ARCHIVE_CONFIG.get("sessionBackupDir") or "").strip()
    if cfg_dir and cfg_dir != "/":
        norm = _archive_norm_dir(cfg_dir)
        if norm and norm != "/":
            return norm, "设置页自定义路径", []

    mounts = await _openlist_mount_names(token) if token else []

    default_dir = str(_ARCHIVE_CONFIG.get("defaultDir") or "").strip()
    default_mount = ""
    if default_dir and default_dir != "/":
        segs = [s for s in default_dir.split("/") if s]
        if segs:
            default_mount = segs[0]

    if token and mounts:
        existing = [m for m in mounts if await _mount_has_backups(token, m)]
        if existing:
            # 归档目录所在网盘若已有备份，优先选它；否则按名称排序取第一个（确定性）
            chosen = default_mount if default_mount in existing else sorted(existing)[0]
            return f"/{chosen}/TG-Backups", f"复用网盘 /{chosen} 上已有的备份目录", mounts
        if default_mount and default_mount in mounts:
            return f"/{default_mount}/TG-Backups", f"跟随归档目录所在网盘 /{default_mount}", mounts
        if len(mounts) == 1:
            return f"/{mounts[0]}/TG-Backups", f"唯一网盘 /{mounts[0]} 自动选择", mounts
        return "", (
            "尚未指定云端备份目录：检测到 " + str(len(mounts)) + " 个网盘（"
            + "、".join("/" + m for m in mounts) + "），请在设置页指定其一"
        ), mounts

    return _SESSION_BACKUP_REMOTE_DIR, "默认常量（未能探测 OpenList 网盘）", mounts


async def _resolve_session_backup_remote_dir(token: Optional[str] = None) -> str:
    """云端冷备目录（兼容旧签名）。解析规则见 _resolve_session_backup_remote_dir_detail。"""
    resolved, _source, _mounts = await _resolve_session_backup_remote_dir_detail(token)
    return resolved


async def _resolve_session_backup_remote_dir_checked(token: Optional[str] = None) -> Tuple[str, str]:
    """返回 (目录, 说明)。目录为空串时，说明即为「为什么无法上传」的用户可读原因。"""
    resolved, source, _mounts = await _resolve_session_backup_remote_dir_detail(token)
    return resolved, source

async def _upload_session_backup_to_openlist(filename: str, enc_bytes: bytes) -> Tuple[bool, str]:
    try:
        ready = await _openlist_ready()
        if not ready:
            return False, "OpenList 服务未配置或未就绪"
        token = await _openlist_token()
        if not token:
            return False, "OpenList 登录凭据无效"

        remote_dir, source = await _resolve_session_backup_remote_dir_checked(token)
        if not remote_dir:
            return False, source
        await _openlist_mkdir_tree(token, remote_dir)

        remote_path = f"{remote_dir}/{filename}"
        headers = {
            "Authorization": token,
            "File-Path": quote(remote_path, safe="/:"),
            "Content-Length": str(len(enc_bytes)),
            "As-Task": "false",
        }
        resp = await _openlist_upload_client.put("/api/fs/put", content=enc_bytes, headers=headers)
        code, _, msg = _openlist_env(resp)
        # OpenList 用「HTTP 200 + 包络码」表达鉴权失败：token 过期时错误体里的
        # code 才是 401/403（见 openlist_service._openlist_put_once 的同款写法）。
        # 老代码只判 resp.status_code == 401，token 失效后冷备永远无法自愈。
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            headers["Authorization"] = token
            resp = await _openlist_upload_client.put("/api/fs/put", content=enc_bytes, headers=headers)
            code, _, msg = _openlist_env(resp)
        if code != 200:
            return False, f"OpenList 返回错误码 {code}: {msg}"

        exists = await _openlist_exists(token, remote_path)
        if not exists:
            return False, "上传完成但云端存在性核验未通过"

        try:
            list_resp = await _openlist_client.post(
                "/api/fs/list",
                json={"path": remote_dir, "password": "", "page": 1, "per_page": 100, "refresh": True},
                headers={"Authorization": token}
            )
            l_code, l_data, _ = _openlist_env(list_resp)
            if l_code == 200 and isinstance(l_data, dict):
                content = l_data.get("content") or []
                backup_files = [f for f in content if isinstance(f, dict) and str(f.get("name") or "").startswith("tg-session-")]
                if len(backup_files) > _SESSION_BACKUP_MAX_KEEP:
                    backup_files.sort(key=lambda x: str(x.get("modified") or ""))
                    to_remove = [f.get("name") for f in backup_files[:len(backup_files) - _SESSION_BACKUP_MAX_KEEP]]
                    await _openlist_client.post(
                        "/api/fs/remove",
                        json={"dir": remote_dir, "names": to_remove},
                        headers={"Authorization": token}
                    )
        except Exception as e:
            log.warning("OpenList 云端冷备轮转警告: %s", e)

        return True, remote_path
    except Exception as e:
        log.warning("上传冷备至 OpenList 失败: %s", e)
        return False, str(e)


async def create_session_backup(local_only: bool = False) -> Dict[str, Any]:
    t0 = time.time()
    ts_str = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
    filename = f"tg-session-{ts_str}.tar.gz.enc"

    tar_bytes, rel_files = await asyncio.to_thread(_create_session_archive_bytes, APP_ROOT_DIR)
    enc_payload = await asyncio.to_thread(_encrypt_session_payload, tar_bytes)
    sha256 = hashlib.sha256(enc_payload).hexdigest()

    # 本地冷备目录：支持设置页自定义路径（任意绝对路径，可指向独立磁盘/挂载卷）。
    # 自定义路径创建失败时必须显式报错，绝不静默回退到默认目录——
    # 否则用户以为备份落到了独立盘，实际还在数据盘上，等故障时才发现。
    local_dir, local_source = _resolve_local_backup_dir()
    try:
        os.makedirs(local_dir, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"本地冷备目录不可用（{local_source}：{local_dir}）：{e}") from e

    local_path = os.path.join(local_dir, filename)
    def _write_local():
        fd = os.open(local_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(enc_payload)
        _rotate_local_backups(local_dir, _SESSION_BACKUP_MAX_KEEP)
    await asyncio.to_thread(_write_local)

    # local_only：只落本地快照，跳过 OpenList 上传。
    if local_only:
        remote_ok, remote_res = False, "已按请求跳过云端上传（仅本地备份）"
    else:
        remote_ok, remote_res = await _upload_session_backup_to_openlist(filename, enc_payload)
    remote_path = remote_res if remote_ok else ""

    status_entry = {
        "ok": True,
        "filename": filename,
        "size": len(enc_payload),
        "remotePath": remote_path,
        "last_backup_time": t0,
        "last_backup_time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
        "last_backup_name": filename,
        "last_backup_size": len(enc_payload),
        "last_backup_size_str": f"{len(enc_payload) / 1024:.1f} KB" if len(enc_payload) < 1024 * 1024 else f"{len(enc_payload) / (1024 * 1024):.2f} MB",
        "sha256": sha256,
        "local_path": local_path,
        "local_dir": local_dir,
        "local_source": local_source,
        "local_only": local_only,
        "remote_path": remote_path,
        "uploaded_to_openlist": remote_ok,
        "uploadedToOpenList": remote_ok,
        "files_count": len(rel_files),
        # 仅本地备份时状态记为 ok（本地快照确实生成了），不是告警。
        "status": "ok" if (remote_ok or local_only) else "warn",
        "message": (
            f"本地冷备完成（{local_dir}），已按请求跳过云端上传"
            if local_only else
            (f"冷备成功完成并已同步至 OpenList ({remote_path})" if remote_ok
             else f"本地冷备完成（{local_dir}），但 OpenList 同步提示: {remote_res}")
        ),
    }
    await _save_session_backup_status(status_entry)

    LOG_STORE.append("INFO", f"TG Session 冷备快照已完成: {filename} ({status_entry['last_backup_size_str']})，异地同步: {'成功' if remote_ok else '本地留存'}")
    return status_entry

# ---------------------------------------------------------------------
# 备份包命名与「会话核心文件」白名单
# ---------------------------------------------------------------------
# bridge 加密包：tg-session-<ts>.tar.gz.enc（单数 session）
# 每日 03:30 脚本明文包：tg-sessions-<ts>.tar.gz（复数 sessions）
_BACKUP_NAME_RE = re.compile(r"^tg-sessions?-\d{8}-\d{6}\.tar\.gz(?:\.enc)?$")

# 会话核心文件白名单（相对数据根目录）。每日脚本产出的是整树明文快照，
# 实测 274 项 / 850 MB（含 photos/、logs/、archive_jobs.json 等），
# 还原必须精确落盘：整包解开会把媒体缓存写回磁盘并覆盖运行中的业务数据。
_SESSION_CORE_ROOT_FILES = frozenset({
    "data.db", "data.db-wal", "data.db-shm",
    ".backend_creds", ".bridge_secret", ".openlist_auth",
    ".subscriptions.json", ".archive_jobs.json", ".archive_config.json",
    ".notify_config.json", ".session_backup_key",
    "session_backup_manifest.json",
})
# 明确排除的目录名：命中即拒绝（媒体本体与 TDLib 缓存不属于会话核心）
_SESSION_FORBIDDEN_SEGMENTS = frozenset({
    "videos", "temp", "photos", "thumbnails", "documents", "downloads", "logs",
})
# 判定「归档确实含会话命脉」的最小证据（缺一即拒绝还原）
_SESSION_REQUIRED_HINT = "td.binlog"


def _fmt_size(n: int) -> str:
    """字节数转人类可读体积（页面展示统一口径）。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / (1024 * 1024):.2f} MB"
    return f"{n / (1024 ** 3):.2f} GB"


def _session_backup_status_file() -> str:
    """状态文件路径：随当前 APP_ROOT_DIR 动态解析，避免单测/迁移时写错位置。"""
    return os.path.join(APP_ROOT_DIR, ".session_backup_status.json")


def _session_backup_key_file() -> str:
    """主密钥文件路径：同上动态解析。"""
    return os.path.join(APP_ROOT_DIR, ".session_backup_key")


def _resolve_local_backup_dir() -> Tuple[str, str]:
    """解析本地冷备目录，返回 (目录, 来源说明)。

    优先级：
      1. 环境变量 TG_SESSION_BACKUP_LOCAL_DIR（部署级覆盖）
      2. 设置页显式指定的自定义路径（localBackupDir，支持任意绝对路径）
      3. 与 app 平级的 session-backups（便于运维直接取包）
      4. 数据目录内的 session-backups（兜底）

    历史行为：目录写死在代码里，用户无法把冷备落到独立磁盘/挂载卷，
    只能跟着 app 数据目录一起冒风险（同盘故障即同时丢失）。
    """
    env_dir = str(os.environ.get("TG_SESSION_BACKUP_LOCAL_DIR") or "").strip()
    if env_dir:
        return env_dir, "环境变量 TG_SESSION_BACKUP_LOCAL_DIR"

    cfg_dir = str(_ARCHIVE_CONFIG.get("localBackupDir") or "").strip()
    if cfg_dir:
        return cfg_dir, "设置页自定义路径"

    parent = os.path.join(os.path.dirname(APP_ROOT_DIR), "session-backups")
    if os.path.isdir(parent):
        return parent, "默认（与 app 平级）"
    return os.path.join(APP_ROOT_DIR, "session-backups"), "默认（数据目录内）"


def session_backup_local_dir() -> str:
    """本地冷备目录（兼容旧签名，仅返回路径）。解析规则见 _resolve_local_backup_dir。"""
    return _resolve_local_backup_dir()[0]

def _validate_member_name(name: str) -> str:
    """校验归档成员名合法（拒绝绝对路径与 ../ 逃逸），返回归一化名。"""
    norm = os.path.normpath(str(name or "")).replace("\\", "/")
    if norm.startswith("/") or norm.startswith("../") or ".." in norm.split("/"):
        raise ValueError(f"备份文件包含非法相对路径逃逸: {name}")
    return norm


def _session_core_rel_path(norm_name: str) -> Optional[str]:
    """归一化成员名 → 会话核心相对路径；非核心文件返回 None。

    兼容两种归档结构：
    - bridge 加密包：成员名本身即相对路径（account/<id>/td.binlog）
    - 每日脚本明文包：整树以 app-data/ 为前缀（app-data/account/<id>/td.binlog）
    """
    rel = norm_name
    while rel.startswith("./"):
        rel = rel[2:]
    if rel == "app-data":
        return None
    if rel.startswith("app-data/"):
        rel = rel[len("app-data/"):]
    segs = [s for s in rel.split("/") if s]
    if not segs:
        return None
    if len(segs) == 1:
        return segs[0] if segs[0] in _SESSION_CORE_ROOT_FILES else None
    # 任何一段命中禁列目录（videos/photos/logs/…）即不还原
    if any(s in _SESSION_FORBIDDEN_SEGMENTS for s in segs[:-1]):
        return None
    # account/<id>/td.binlog —— 只认授权密钥本体，不收 db.sqlite 等 Chat 缓存
    if len(segs) >= 3 and segs[0] == "account" and segs[-1] == "td.binlog":
        return "/".join(segs)
    return None


def _extract_session_core(tar: tarfile.TarFile, target_dir: str) -> List[str]:
    """按白名单从归档中精确提取会话核心文件，返回落盘的相对路径列表。"""
    members = tar.getmembers()
    if not members:
        raise ValueError("备份归档为空，无法还原")

    picked: List[Tuple[tarfile.TarInfo, str]] = []
    for m in members:
        # 路径穿越必须先于白名单过滤判定，否则逃逸成员会被静默丢弃而非报错
        norm = _validate_member_name(m.name)
        if m.isdir():
            continue
        rel = _session_core_rel_path(norm)
        if rel is not None:
            picked.append((m, rel))

    if not any(rel.endswith(_SESSION_REQUIRED_HINT) for _, rel in picked):
        raise ValueError("备份归档不含有效的 TDLib 会话核心文件 (td.binlog/.backend_creds)")

    os.makedirs(target_dir, exist_ok=True)
    restored: List[str] = []
    for m, rel in picked:
        src = tar.extractfile(m)
        if src is None:
            continue
        dest = os.path.join(target_dir, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(dest, 0o600)
        except Exception as e:  # noqa: BLE001
            log.warning("还原文件失败 %s: %s", rel, e)
            continue
        restored.append(rel)

    if not restored:
        raise ValueError("备份归档不含有效的 TDLib 会话核心文件 (td.binlog/.backend_creds)")
    return restored

def restore_session_backup(source: Union[str, bytes], target_dir: str = APP_ROOT_DIR, master_key: Optional[bytes] = None) -> Dict[str, Any]:
    """解密并还原 bridge 自产的 AES-256-GCM 加密快照（.tar.gz.enc）。"""
    if isinstance(source, str):
        if not os.path.exists(source):
            raise FileNotFoundError(f"备份文件不存在: {source}")
        with open(source, "rb") as f:
            enc_bytes = f.read()
    else:
        enc_bytes = bytes(source)

    plain_bytes = _decrypt_session_payload(enc_bytes, master_key=master_key)
    with tarfile.open(fileobj=io.BytesIO(plain_bytes), mode="r:gz") as tar:
        restored_names = _extract_session_core(tar, target_dir)

    return {
        "ok": True,
        "restored_count": len(restored_names),
        "files": restored_names,
        "target_dir": target_dir,
        "message": f"成功解密并还原 {len(restored_names)} 个会话核心文件",
    }


def restore_session_backup_plain(path: str, target_dir: str = APP_ROOT_DIR) -> Dict[str, Any]:
    """还原每日脚本产出的明文快照（tg-sessions-*.tar.gz），按白名单精确落盘。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"备份文件不存在: {path}")
    with tarfile.open(path, mode="r:gz") as tar:
        restored_names = _extract_session_core(tar, target_dir)
    return {
        "ok": True,
        "restored_count": len(restored_names),
        "files": restored_names,
        "target_dir": target_dir,
        "message": f"成功还原 {len(restored_names)} 个会话核心文件（明文快照）",
    }


def restore_session_backup_file(path: str, target_dir: str = APP_ROOT_DIR) -> Dict[str, Any]:
    """按文件真实内容自动选择还原通道（不靠扩展名猜）。

    以 TGSNAP01 开头 → AES-256-GCM 加密包；
    以 gzip 魔数 1f 8b 开头 → 明文 tar.gz（每日脚本快照）。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"备份文件不存在: {path}")
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(_SESSION_BACKUP_MAGIC):
        return restore_session_backup(path, target_dir=target_dir)
    if head[:2] == b"\x1f\x8b":
        return restore_session_backup_plain(path, target_dir=target_dir)
    raise ValueError("无法识别的备份包格式：既不是 TGSNAP01 加密包，也不是 gzip 明文快照")


async def _save_session_backup_status(data: Dict[str, Any]) -> None:
    """持久化冷备健康状态"""
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_session_backup_status_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:
        log.warning("保存备份状态文件失败: %s", e)



async def _docker_container_action(action: str) -> Tuple[bool, str]:
    """对后端容器执行 stop/start（还原会话前必须先停写）。

    用参数数组调用，杜绝 shell 注入；docker 不存在时返回 ok=False 交由调用方判定。
    """
    exe = shutil.which("docker")
    if not exe:
        return False, "本机未安装 docker，无法自动停写后端容器"
    container = str(_SESSION_BACKUP_CONTAINER or "")
    if not container:
        return False, "未配置后端容器名，无法自动停写"
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, action, container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0:
            return True, text or f"{action} {container} 成功"
        return False, text or f"{action} {container} 退出码 {proc.returncode}"
    except asyncio.TimeoutError:
        return False, f"{action} {container} 超时（120s）"
    except Exception as e:  # noqa: BLE001
        return False, f"{action} {container} 异常: {e}"


async def restore_session_backup_async(name: str, origin: str = "local", confirm: bool = False) -> Dict[str, Any]:
    """一键还原编排：校验 → 还原前回滚快照 → 停写后端容器 → 精确落盘 → 重新启动。

    为什么必须停容器：容器把 app-data 挂载为 /app/data，运行中的 TDLib 持有
    td.binlog/data.db 并持续回写，直接覆盖会被立即改回，属于静默失败。
    """
    if not confirm:
        return {"ok": False, "message": "还原为破坏性操作，需要二次确认"}
    bname = os.path.basename(str(name or "").strip())
    if not _BACKUP_NAME_RE.match(bname):
        return {"ok": False, "message": "备份包名称非法，仅接受 tg-session(s)-<日期>-<时间>.tar.gz[.enc]"}

    t0 = time.time()
    origin = "remote" if str(origin).lower() == "remote" else "local"
    local_dir = session_backup_local_dir()
    os.makedirs(local_dir, exist_ok=True)

    # 1) 定位还原源（云端包先取回到本地 fetched 缓存目录）
    path = os.path.join(local_dir, bname)
    if origin == "remote":
        try:
            remote_dir = await _resolve_session_backup_remote_dir(await _openlist_token())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"OpenList 不可用，无法从云端取回：{e}"}
        path = os.path.join(local_dir, "fetched", bname)
        ok, msg = await openlist_download_to_file(f"{remote_dir}/{bname}", path)
        if not ok:
            return {"ok": False, "message": f"从 OpenList 取回备份包失败：{msg}"}
    elif not os.path.isfile(path):
        return {"ok": False, "message": f"本地不存在该备份包：{bname}"}

    # 2) 还原前快照：任何还原都必须留下可回滚的一份
    pre_name = ""
    if _SESSION_BACKUP_PRERESTORE:
        try:
            pre = await create_session_backup()
            pre_name = str(pre.get("filename") or "")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"还原前回滚快照生成失败，已中止还原：{e}"}

    # 3) 停写后端容器
    stopped = False
    ok_stop, stop_msg = await _docker_container_action("stop")
    if ok_stop:
        stopped = True
    elif "未安装 docker" in stop_msg or "未配置" in stop_msg:
        log.warning("还原未停写后端容器：%s", stop_msg)
    else:
        return {"ok": False, "prerestoreName": pre_name,
                "message": f"停写后端容器失败，已中止还原（避免半写状态）：{stop_msg}"}

    # 4) 精确落盘
    try:
        res = await asyncio.to_thread(restore_session_backup_file, path, APP_ROOT_DIR)
    except Exception as e:  # noqa: BLE001
        if stopped:
            await _docker_container_action("start")
        LOG_STORE.append("ERROR", f"Session 还原失败: {bname} → {e}")
        return {"ok": False, "message": f"还原失败：{e}", "prerestoreName": pre_name,
                "containerRestarted": stopped}

    # 5) 恢复服务
    if stopped:
        restart_ok, restart_msg = await _docker_container_action("start")
    else:
        restart_ok, restart_msg = True, "未停写容器，无需重启"

    duration = time.time() - t0
    await _record_restore_result(bname, origin, res, duration, restart_ok, pre_name)
    LOG_STORE.append(
        "INFO",
        f"Session 还原完成: {bname}（{res.get('restored_count')} 个核心文件，耗时 {duration:.2f}s，来源 {origin}）")
    if stopped and not restart_ok:
        log.error("还原后重启后端容器失败: %s", restart_msg)
    return {
        "ok": True,
        "name": bname,
        "origin": origin,
        "restoredCount": res.get("restored_count"),
        "files": res.get("files"),
        "durationSec": round(duration, 2),
        "prerestoreName": pre_name,
        "containerStopped": stopped,
        "containerRestarted": restart_ok,
        "message": f"已还原 {res.get('restored_count')} 个会话核心文件（耗时 {duration:.2f}s）"
                   + ("，后端容器已重启" if stopped and restart_ok else "")
                   + ("" if restart_ok else f"；但后端容器重启失败：{restart_msg}"),
    }


async def _record_restore_result(name: str, origin: str, res: Dict[str, Any], duration: float,
                                 restarted: bool, pre_name: str) -> None:
    """把最近一次还原结果合并进状态文件（供页面显示，不覆盖冷备字段）。"""
    try:
        st: Dict[str, Any] = {}
        sfile = _session_backup_status_file()
        if os.path.exists(sfile):
            try:
                with open(sfile, "r", encoding="utf-8") as f:
                    st = json.loads(f.read())
            except Exception:
                st = {}
        st.update({
            "last_restore_time": time.time(),
            "last_restore_time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "last_restore_name": name,
            "last_restore_origin": origin,
            "last_restore_count": res.get("restored_count"),
            "last_restore_duration": round(duration, 2),
            "last_restore_restarted": restarted,
            "last_restore_prerestore": pre_name,
        })
        await _save_session_backup_status(st)
    except Exception as e:  # noqa: BLE001
        log.warning("记录还原结果失败: %s", e)


async def list_session_backups() -> Dict[str, Any]:
    """列出全部真实可还原的快照（本地加密包 + 本地明文包 + OpenList 云端包）。

    全部来自真实文件系统与 OpenList 接口，绝不做任何硬编码兜底。
    """
    items: List[Dict[str, Any]] = []
    local_dir = session_backup_local_dir()
    if os.path.isdir(local_dir):
        try:
            for f in sorted(os.listdir(local_dir)):
                full = os.path.join(local_dir, f)
                if not os.path.isfile(full) or not _BACKUP_NAME_RE.match(f):
                    continue
                try:
                    stt = os.stat(full)
                except OSError:
                    continue
                items.append({
                    "name": f,
                    "origin": "local",
                    "encrypted": f.endswith(".enc"),
                    "size": stt.st_size,
                    "sizeStr": _fmt_size(stt.st_size),
                    "mtimeStr": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stt.st_mtime)),
                    "mtime": stt.st_mtime,
                })
        except Exception as e:  # noqa: BLE001
            log.warning("扫描本地冷备目录失败: %s", e)

    remote_dir = ""
    remote_error = ""
    remote_source = ""
    remote_mounts: List[str] = []
    try:
        remote_dir, remote_source, remote_mounts = await _resolve_session_backup_remote_dir_detail(
            await _openlist_token())
        if not remote_dir:
            # 多网盘且无法确定目标：如实告知并给出候选，绝不猜一个写进去
            remote_error = remote_source
            lst = {"ok": False}
        else:
            lst = await openlist_list_files(remote_dir)
        if lst.get("ok"):
            local_names = {it["name"] for it in items}
            for rf in lst.get("files") or []:
                rname = str(rf.get("name") or "")
                if not _BACKUP_NAME_RE.match(rname):
                    continue
                items.append({
                    "name": rname,
                    "origin": "remote",
                    "encrypted": rname.endswith(".enc"),
                    "size": int(rf.get("size") or 0),
                    "sizeStr": _fmt_size(int(rf.get("size") or 0)),
                    "mtimeStr": str(rf.get("modified") or "").replace("T", " ").replace("Z", ""),
                    "mtime": 0.0,
                    "alsoLocal": rname in local_names,
                })
        elif not remote_error:
            remote_error = str(lst.get("message") or "")
    except Exception as e:  # noqa: BLE001
        remote_error = str(e)

    items.sort(key=lambda x: (str(x.get("mtimeStr") or ""), x.get("name") or ""), reverse=True)
    local_dir, local_source = _resolve_local_backup_dir()
    return {
        "ok": True,
        "remoteDir": remote_dir,
        "savedDir": str(_ARCHIVE_CONFIG.get("sessionBackupDir") or ""),
        "localDirSaved": str(_ARCHIVE_CONFIG.get("localBackupDir") or ""),
        "localDir": local_dir,
        "localSource": local_source,
        "localWritable": os.path.isdir(local_dir) and os.access(local_dir, os.W_OK),
        "remoteSource": remote_source,
        "remoteMounts": remote_mounts,
        "remoteError": remote_error,
        "needsConfig": bool(remote_mounts) and not remote_dir,
        "items": items,
        "localCount": len([i for i in items if i["origin"] == "local"]),
        "remoteCount": len([i for i in items if i["origin"] == "remote"]),
    }


def _backup_name_ok(name: str) -> bool:
    """严格校验快照文件名。

    该名字直接拼进本地路径 / 传给 OpenList 删除接口，必须限定为**单一段**文件名：
    过 _BACKUP_NAME_RE 且不含任何路径分隔符或 .. —— 否则 name 里塞 "../" 就能删到
    备份目录之外的文件。
    """
    n = str(name or "").strip()
    if not n or n in (".", ".."):
        return False
    if "/" in n or "\\" in n or "\x00" in n:
        return False
    try:
        if os.path.basename(n) != n:
            return False
    except Exception:  # noqa: BLE001
        return False
    return bool(_BACKUP_NAME_RE.match(n))


async def delete_session_backup(name: str, origin: str = "local") -> Dict[str, Any]:
    """手动删除单个会话快照。

    origin='local'  -> 删除本地快照文件
    origin='remote' -> 删除 OpenList 云端快照（先解析当前生效的冷备目录）

    只删用户点的那一份，绝不顺带做轮转/清理 —— 轮转由 _rotate_local_backups
    与上传后的云端轮转各自负责，混在一起会让「我只要删一份」变成删一批。
    """
    nm = str(name or "").strip()
    if not _backup_name_ok(nm):
        return {"ok": False, "message": "快照文件名无效"}

    org = str(origin or "local").strip().lower()

    if org == "local":
        local_dir = session_backup_local_dir()
        full = os.path.join(local_dir, nm)
        if not os.path.isfile(full):
            return {"ok": False, "message": "本地快照不存在（可能已被删除或轮转）"}
        try:
            size = os.path.getsize(full)
            os.remove(full)
        except OSError as e:
            log.warning("删除本地快照失败 %s: %s", nm, e)
            return {"ok": False, "message": f"删除失败：{e}"}
        log.info("已手动删除本地会话快照: %s (%d 字节)", nm, size)
        return {"ok": True, "origin": "local", "name": nm, "message": f"已删除本地快照 {nm}"}

    if org == "remote":
        remote_dir, remote_source, _mounts = await _resolve_session_backup_remote_dir_detail(
            await _openlist_token())
        if not remote_dir:
            return {"ok": False, "message": remote_source or "无法确定云端冷备目录"}
        try:
            token = await _openlist_token()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"OpenList 未就绪：{e}"}

        async def _rm_once(tok: str):
            return await _openlist_client.post(
                "/api/fs/remove",
                json={"dir": remote_dir, "names": [nm]},
                headers={"Authorization": tok}
            )

        try:
            resp = await _rm_once(token)
            code, _data, msg = _openlist_env(resp)
            if resp.status_code == 401 or code in (401, 403):
                token = await _openlist_relogin()
                resp = await _rm_once(token)
                code, _data, msg = _openlist_env(resp)
            low = (msg or "").lower()
            if code != 200 and "not found" not in low and "no such file" not in low:
                return {"ok": False, "message": f"云端删除失败：{msg or f'OpenList 业务码 {code}'}"}
        except Exception as e:  # noqa: BLE001
            log.warning("删除云端快照失败 %s: %s", nm, e)
            return {"ok": False, "message": f"云端删除失败：{e}"}
        log.info("已手动删除云端会话快照: %s/%s", remote_dir, nm)
        return {"ok": True, "origin": "remote", "name": nm,
                "message": f"已删除云端快照 {nm}"}

    return {"ok": False, "message": "origin 只能是 local 或 remote"}


async def _get_session_backup_status() -> Dict[str, Any]:
    """读取并判定冷备健康指示灯、最近备份与最近还原情况。

    健康判定不只看状态文件里那一次手动记录：每日 03:30 的 systemd 定时脚本
    产出的是 tg-sessions-*.tar.gz（明文、仅本地），历史实现只统计 .enc，
    导致明明每天都有新快照却长期误报「建议备份」。
    """
    now = time.time()
    st: Dict[str, Any] = {}
    sfile = _session_backup_status_file()
    if os.path.exists(sfile):
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                st = json.loads(f.read())
        except Exception as e:
            log.warning("读取备份状态文件失败: %s", e)

    lst = await list_session_backups()
    items: List[Dict[str, Any]] = list(lst.get("items") or [])
    remote_dir = str(lst.get("remoteDir") or _SESSION_BACKUP_REMOTE_DIR)
    local_items = [i for i in items if i.get("origin") == "local"]
    remote_items = [i for i in items if i.get("origin") == "remote"]
    latest_local = max(local_items, key=lambda x: float(x.get("mtime") or 0), default=None)

    recorded_time = float(st.get("last_backup_time") or 0)
    local_time = float((latest_local or {}).get("mtime") or 0)
    last_time = max(recorded_time, local_time)
    # 异地冗余必须以真实探测结果为准：
    # 云端可枚举时，只有确实存在远端快照才算已冗余（否则删掉云端副本后，
    # 状态文件里的历史 uploaded_to_openlist 会让卡片继续谎报「冷备正常」）。
    # 只有云端不可达（无法判定）时，才退回状态文件里的历史标记。
    if not lst.get("remoteError"):
        uploaded = bool(remote_items)
    else:
        uploaded = bool(st.get("uploaded_to_openlist", False))

    if latest_local and local_time >= recorded_time:
        last_name = str(latest_local.get("name") or "")
        last_size_str = str(latest_local.get("sizeStr") or "")
        last_time_str = str(latest_local.get("mtimeStr") or "")
    else:
        last_name = str(st.get("last_backup_name") or "")
        last_size_str = str(st.get("last_backup_size_str") or "")
        last_time_str = str(st.get("last_backup_time_str") or "")

    if last_time <= 0:
        status = dot_class = "warn"
        label = "尚未备份"
    else:
        age_hours = (now - last_time) / 3600.0
        # 每日 03:30 定时快照间隔为 24h，留 6h 抖动余量避免误报
        if age_hours <= 30.0:
            if uploaded:
                status = dot_class = "ok"
                label = "冷备正常"
            else:
                status = dot_class = "warn"
                label = "仅本地留存"
        else:
            status = dot_class = "warn"
            label = "建议备份"

    if last_name:
        msg = f"最近快照 {last_name}（{last_time_str}，{last_size_str}）· 本地 {len(local_items)} 份"
    else:
        msg = "尚未生成任何会话快照，建议点击「立即备份」"
    if lst.get("remoteError"):
        msg += f" · 云端不可用：{lst['remoteError']}"
    else:
        msg += f" · 云端 {remote_dir} {len(remote_items)} 份"
    if st.get("last_restore_time_str"):
        msg += f" · 最近还原 {st['last_restore_time_str']}"

    return {
        "ok": True,
        "status": status,
        "dotClass": dot_class,
        "label": label,
        "lastBackupTime": last_time_str,
        "lastBackupTimestamp": last_time,
        "lastBackupSize": last_size_str,
        "lastBackupName": last_name,
        "remoteDir": remote_dir,
        "remotePath": st.get("remote_path") or "",
        "backupCount": len(local_items),
        "localCount": len(local_items),
        "remoteCount": len(remote_items),
        "uploadedToOpenList": uploaded,
        # 如实告知：本地每日 03:30 自动快照；推送到 OpenList 只在手动点击时发生
        "dailyLocalBackup": True,
        "remoteAutoPush": False,
        "lastRestoreTime": st.get("last_restore_time_str") or "",
        "lastRestoreName": st.get("last_restore_name") or "",
        "message": msg,
    }
