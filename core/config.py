# -*- coding: utf-8 -*-
"""
core/config.py — 系统全局配置常量、环境变量与基础工具函数
=========================================================
本模块为底层核心配置模块，只依赖 Python 标准库，无任何上层模块反向依赖。
"""
import os
import re
import time
import posixpath
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------
# 目录与环境配置
# ---------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

TG_API_URL = os.environ.get("TG_API_URL", "http://127.0.0.1:8123/api").rstrip("/")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8000"))
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
SECURE_COOKIE = os.environ.get("BRIDGE_SECURE_COOKIE", "") == "1"
CACHE_TTL = float(os.environ.get("BRIDGE_CACHE_TTL", "8"))       # 秒，短 TTL
WS_RECONNECT_DELAY = float(os.environ.get("BRIDGE_WS_DELAY", "3"))

LOGIN_RATE_LIMIT = int(os.environ.get("BRIDGE_LOGIN_LIMIT", "10"))
LOGIN_RATE_WINDOW = float(os.environ.get("BRIDGE_LOGIN_WINDOW", "300"))
TRUSTED_PROXIES = [s.strip() for s in os.environ.get("BRIDGE_TRUSTED_PROXIES", "").split(",") if s.strip()]
_LOGIN_FAILURE_MAX = 10000

APP_ROOT_DIR = os.environ.get("TG_DATA_DIR", "/root/tg-files/app-data")

# ---------------------------------------------------------------------
# 测试进程护栏：测试运行绝不允许写生产 app-data
# ---------------------------------------------------------------------
# 背景：本模块的 APP_ROOT_DIR 是导入期一次性求值的常量，一旦被导入就固化。
# `unittest discover -s tests` 会把测试模块当作**顶层模块**按字母序导入，
# 因此 tests/__init__.py 与 tests/conftest.py 都不会执行（unittest 不读 conftest，
# 顶层导入也不走包 __init__）。于是字母序靠前的测试模块（如 test_account_health_page）
# 先 `import bridge_server` → `import core.config`，把 APP_ROOT_DIR 固化为生产目录；
# 之后 test_subscriptions_e2e 再设置 TG_DATA_DIR 临时目录就完全失效，
# 它的 _subs_save()/_archive_save() 会直接写生产 app-data（曾造成 mock 残留，
# 以及近 5000 条 f<N>.mp4 测试条目把 /library/cloud 撑到 19.5MB）。
#
# 判据：unittest 的测试运行进程必定已导入 unittest 模块；生产服务进程
# （uvicorn 启动 bridge_server）不会导入它。该判据在测试模块导入期采样，
# 所以必须写在本模块顶部、紧随 APP_ROOT_DIR 定义之后。
#
# 策略（安全优先且不破坏测试）：
#   * 测试进程中若 TG_DATA_DIR 未指定 → 自动改用独立临时目录（进程退出清理）。
#   * 测试进程中若 TG_DATA_DIR 明确指向生产目录 → 直接报错中止。
import sys as _sys

_PROD_DATA_DIR = "/root/tg-files/app-data"
if "unittest" in _sys.modules:
    if os.path.abspath(APP_ROOT_DIR) == os.path.abspath(_PROD_DATA_DIR):
        if os.environ.get("TG_DATA_DIR"):
            # 显式指定成了生产目录 —— 宁可失败也不许写真实数据
            raise RuntimeError(
                "测试隔离失败：TG_DATA_DIR 指向生产数据目录 "
                f"{_PROD_DATA_DIR!r}。测试会覆盖真实数据，已中止。"
            )
        # 未指定：自动落到独立临时目录，保护生产数据
        import tempfile as _tempfile

        _test_data_dir = _tempfile.mkdtemp(prefix="tg-bridge-tests-")
        os.environ["TG_DATA_DIR"] = _test_data_dir
        APP_ROOT_DIR = _test_data_dir

OPENLIST_URL = os.environ.get("OPENLIST_URL", "http://127.0.0.1:5244").rstrip("/")

# ---------------------------------------------------------------------
# 安全凭据与认证参数
# ---------------------------------------------------------------------
PORTAL_COOKIE = "tf_portal"
CSRF_COOKIE = "tf_portal_csrf"
CSRF_HEADER = "X-CSRF-Token"

_SECRET_FILE = os.path.join(APP_ROOT_DIR, ".bridge_secret")
_INIT_FLAG_FILE = os.path.join(APP_ROOT_DIR, ".bridge_initialized")
PORTAL_TTL = 7 * 24 * 3600  # 7 天

PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/login", "/init", "/health"}

def _ws_url() -> str:
    base = TG_API_URL
    if base.endswith("/api"):
        base = base[: -len("/api")]
    if base.startswith("https"):
        base = "ws" + base[len("https"):]
    elif base.startswith("http"):
        base = "ws" + base[len("http"):]
    return base

WS_BASE_URL = _ws_url()

CSRF_WHITELIST = {"/auth/bootstrap", "/auth/login"}

TG_API_METHOD_WHITELIST = {
    "SetAuthenticationPhoneNumber",
    "CheckAuthenticationCode",
    "CheckAuthenticationPassword",
    "CheckAuthenticationBotToken",
    "SetAuthenticationEmailAddress",
    "CheckAuthenticationEmailCode",
    "GetMe",
    "GetAuthorizationState",
    "getAuthorizationState",
    "GetRemoteFile",
    "GetMessage",
    "DownloadFile",
    "SendMessage",
}

# ---------------------------------------------------------------------
# TDLib 状态码与常量
# ---------------------------------------------------------------------
TG_WAIT_PHONE = 306402531
TG_WAIT_CODE = 52643073
TG_WAIT_PASSWORD = 112238030
TG_WAIT_OTHER_DEVICE = 860166378
TG_READY = -1834871737
TG_LOGGING_OUT = 154449270
TG_CLOSING = 445855311
TG_CLOSED = 1526047584

TG_STATE_NAMES = {
    TG_WAIT_PHONE: "WAIT_PHONE_NUMBER",
    TG_WAIT_CODE: "WAIT_CODE",
    TG_WAIT_PASSWORD: "WAIT_PASSWORD",
    TG_WAIT_OTHER_DEVICE: "WAIT_OTHER_DEVICE_CONFIRMATION",
    TG_READY: "READY",
    TG_LOGGING_OUT: "LOGGING_OUT",
    TG_CLOSING: "CLOSING",
    TG_CLOSED: "CLOSED",
}

# ---------------------------------------------------------------------
# 日志格式
# ---------------------------------------------------------------------
_LOG_TIME_FMT = "%m-%d %H:%M:%S"
_LOG_STORE_LEVELS = ("INFO", "WARN", "ERROR")

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# 持久化文件与水位参数
# ---------------------------------------------------------------------
_CREDS_FILE = os.path.join(APP_ROOT_DIR, ".backend_creds")

_WAITING_DISK_FILE = os.path.join(APP_ROOT_DIR, ".waiting_disk.json")
_WAITING_DISK_TASKS_MAX = 500
# 注意：磁盘水位阈值不在此处定义。真实生效值来自运行时可变配置
# _ARCHIVE_CONFIG["diskHighWatermarkPercent" / "diskLowWatermarkPercent"]
# （core/state.py，可在设置页调整并持久化到 .archive_config.json）。
# 曾在此处放置名为 _DISK_HIGH/LOW_WATERMARK_PERCENT 的常量，但它们从未被引用，
# 且默认值与真实值不一致，容易让运维误以为改这里能调水位 —— 已移除。

_ARCHIVE_FILE = os.path.join(APP_ROOT_DIR, ".archive_jobs.json")
_ARCHIVE_CFG_FILE = os.path.join(APP_ROOT_DIR, ".archive_config.json")
_ARCHIVE_CONFIG_FILE = _ARCHIVE_CFG_FILE
# 归档任务上限与并发信号量的真实定义在 core/state.py（_ARCHIVE_MAX_JOBS=5000、
# asyncio.Semaphore(2)），曾在此重复定义 _ARCHIVE_JOBS_MAX=1000 与
# _ARCHIVE_SEMAPHORE_LIMIT=2，与实际生效值矛盾且从未被引用 —— 已移除。

_RETRIEVE_FILE = os.path.join(APP_ROOT_DIR, ".retrieve_jobs.json")
_RETRIEVE_JOBS_MAX = 500
# _RETRIEVE_SEMAPHORE_LIMIT 的真实定义在 services/retrieve_service.py —— 已移除。

_SUBS_FILE = os.path.join(APP_ROOT_DIR, ".subscriptions.json")
_SUBS_RULES_MAX = 100

_NOTIFY_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".notify_config.json")
_NOTIFY_FILE = _NOTIFY_CONFIG_FILE
_OPENLIST_FILE = os.path.join(APP_ROOT_DIR, ".openlist_auth")

_SESSION_BACKUP_STATUS_FILE = os.path.join(APP_ROOT_DIR, ".session_backup_status.json")
_SESSION_BACKUP_REMOTE_DIR = "/TG-Backups"
_SESSION_BACKUP_SECRET_FILE = os.path.join(APP_ROOT_DIR, ".session_backup_key")
_SESSION_BACKUP_KEY_FILE = _SESSION_BACKUP_SECRET_FILE
_SESSION_BACKUP_MAX_KEEP = 7
# 还原会话时必须先停掉写入 td.binlog/data.db 的后端容器（docker 容器名，可用环境变量覆盖）。
# 不停容器直接覆盖会被运行中的 TDLib 进程立即回写，属于静默失败。
_SESSION_BACKUP_CONTAINER = os.environ.get("TG_BACKUP_CONTAINER", "tg-files-api")
# 还原动作默认是否先自动生成一份「还原前回滚快照」
_SESSION_BACKUP_PRERESTORE = os.environ.get("TG_BACKUP_PRERESTORE", "1") not in ("0", "false", "False")

_FLOOD_WAIT_FILE = os.path.join(APP_ROOT_DIR, ".flood_wait_state.json")

_PROTECTED_MEDIA_EXTENSIONS = (
    ".py", ".json", ".jsonl", ".db", ".sqlite", ".sh", ".env", ".key",
    ".pem", ".yml", ".yaml", ".conf", ".ini", ".log", ".bat", ".cmd"
)

FLOOD_WAIT_REGEX = re.compile(r"(?:FLOOD_WAIT_|retry after |flood wait of )(\d+)", re.IGNORECASE)

# ---------------------------------------------------------------------
# 基础纯工具函数
# ---------------------------------------------------------------------
def _pick(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """从 dict 中按候选 key 依次取第一个非 None 值（防御后端字段名不一致）。"""
    if not isinstance(obj, dict):
        return default
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "":
            return v
    return default


def _fmt_size(size: Any) -> str:
    """把字节数格式化为人类可读（如 1.7 GB）。已是字符串时原样回退。"""
    if size is None:
        return "—"
    if isinstance(size, str):
        return size
    try:
        num = float(size)
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return "{:.0f} {}".format(num, unit) if unit == "B" else "{:.1f} {}".format(num, unit)
        num /= 1024.0
    return "{:.1f} PB".format(num)


def _fmt_time(ts: Any) -> str:
    """把后端时间戳/字符串时间格式化为 HH:MM:SS 供表格显示；无效则回退'—'。"""
    if not ts:
        return "—"
    if isinstance(ts, (int, float)):
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(ts))
        except (OverflowError, OSError, ValueError):
            return "—"
    s = str(ts)
    if "T" in s:
        s = s.replace("T", " ")
    parts = s.split(" ")
    tail = parts[-1]
    tail = tail.split(".")[0][:8]
    if len(tail) == 5:
        tail += ":00"
    if len(parts) >= 2:
        date = parts[0][5:]
        return "%s %s" % (date, tail[:5])
    return tail[:5] or "—"


def _fmt_dur(ms: Any) -> str:
    """毫秒耗时 → 人类可读（如 2m 41s）；无效/负值回退 '—'。"""
    try:
        d = float(ms)
    except (TypeError, ValueError):
        return "—"
    if d < 0:
        return "—"
    secs = int(round(d / 1000.0))
    if secs < 60:
        return "%.0fs" % (d / 1000.0)
    if secs < 3600:
        return "%dm %02ds" % (secs // 60, secs % 60)
    return "%dh %02dm" % (secs // 3600, (secs % 3600) // 60)


def _pick_id(rec: Dict[str, Any]) -> Any:
    """文件记录的稳定唯一标识（FileRecord.uniqueId，TDLib remote id）。"""
    return _pick(rec, "uniqueId", "unique_id", "id", "fileId", "file_id", "messageId", "message_id")


def _human_name(rec: Dict[str, Any]) -> str:
    """后端可能用 fileName/title/name，取一个可靠的显示名。"""
    name = _pick(rec, "fileName", "filename", "title", "name", "message", default="")
    if name:
        return str(name)
    chat = _pick(rec, "chatTitle", "chat_title", "chatName", "channel", default="")
    return "文件 " + str(_pick_id(rec) or (chat or ""))


def _chat_title(rec: Dict[str, Any]) -> str:
    return str(_pick(rec, "chatTitle", "chat_title", "chatName", "channel", default=""))


def _is_safe_subpath(child: str, parent: str) -> bool:
    """严格校验 child 是否在 parent 授权目录树下，防 ../ 路径穿越及同前缀目录越界。"""
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return os.path.commonpath([c, p]) == p
    except Exception:
        return False


def _resolve_host_local_path(lp: str) -> str:
    """兼容 Docker 容器路径：后端容器内 /app/data/... 映射到宿主机 APP_ROOT_DIR/..."""
    lp = str(lp or "").strip()
    if not lp or lp == "—":
        return ""
    if os.path.exists(lp):
        return lp
    if lp.startswith("/app/data/"):
        candidate = os.path.join(APP_ROOT_DIR, lp[len("/app/data/"):])
        if os.path.exists(candidate):
            return candidate
    return lp


def _norm_remote_path(p: str) -> Optional[str]:
    """校验并归一化远端路径：必须 / 开头，拒绝 .. 与反斜杠。"""
    p = (p or "").strip().replace("\\", "/")
    if not p.startswith("/"):
        return None
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    return "/" + "/".join(parts)


def _archive_norm_dir(p: str) -> Optional[str]:
    """归一化 OpenList 目标目录：必须 / 开头，拒绝 .. 与反斜杠，空段折叠，防御 URL 编码穿透。"""
    p = (p or "").strip().replace("\\", "/")
    if "%2e" in p.lower() or "%2f" in p.lower():
        return None
    if not p.startswith("/"):
        return None
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    return "/" + "/".join(parts)


def _archive_join(remote_dir: str, filename: str) -> str:
    """目录 + 文件名 → 完整远端路径（文件名里的 / \\ 落成 _，消除 .. 逃逸，防路径逃逸）。"""
    safe_name = str(filename or "").replace("/", "_").replace("\\", "_").strip() or "未命名"
    while ".." in safe_name:
        safe_name = safe_name.replace("..", "__")
    if not safe_name.strip("._"):
        safe_name = "未命名"
    base = "" if remote_dir == "/" else remote_dir
    return f"{base}/{safe_name}"



# 方括号广告标签：要求括号内确实含广告特征词
_AD_BRACKET_RE = re.compile(
    r"[【\[（\(][^】\]）\)]*(?:电报|群|发布|分享|关注|首发|唯一|永久|地址|资源|频道|水印|防失联|防封|公众号|微信|企鹅|招商|@|t\.me|http)[^】\]）\)]*[】\]）\)]",
    re.IGNORECASE,
)
# 网址 / 域名：整段匹配（含 TLD 与路径）。
# 关键约束：TLD 前的标签必须至少含一个字母，否则「电影.2024.xyz」这种
# 纯数字标签会被当成域名啃掉年份；并且绝不能只匹配一半留下 `.com` 残渣。
_AD_URL_RE = re.compile(
    r"(?:https?://)?(?:"
    r"t\.me(?:/[^\s]*)?"
    r"|www\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+"
    r"|[A-Za-z0-9_\-]*[A-Za-z_\-][A-Za-z0-9_\-]*\.(?:com|net|org|xyz|top|cc|me|vip|tv|cn|io)(?:/[^\s]*)?"
    r")",
    re.IGNORECASE,
)


def _clean_archive_filename(raw_name: str, enabled: bool = True) -> str:
    """清洗归档文件名中的 Telegram 广告尾巴、推广群号、推广括号等，
    便于 Emby / Jellyfin / Infuse / 播放器完美刮削识别。

    两条硬约束（都来自真实的数据丢失事故）：

    1. **绝不能吃掉名字主体。** 旧实现在群号规则里写 `(?:电报群?|TG群?|资源群?|发布群?|...)`，
       `群?` 让「群」变成可选，于是独立的「资源」「发布」「首发」「电报」「TG」
       等完全正常的词也被整段删除：
           `资源 2024 1080p.mp4`  ->  `2024 1080p.mp4`   （名字被吃掉）
       域名规则 `www\\.[a-z0-9_]+` 不含 TLD，还会把 `www.example.com.mp4`
       啃掉一半变成 `com.mp4`。

    2. **必须保留扩展名。** 扩展名在清洗前先切分、清洗后原样拼回；
       顺序绝不能反，否则广告规则会顺着名字一路吃到后缀上。
    """
    if not enabled or not raw_name:
        return raw_name
    name, ext = posixpath.splitext(raw_name)
    if not ext:
        # 无扩展名：整体当作名字处理，且不要用 raw_name 覆盖（多后缀名会错位）
        name, ext = raw_name, ""
    original = name

    # 1) 方括号广告标签
    name = _AD_BRACKET_RE.sub(" ", name)
    # 2) @频道句柄
    name = re.sub(r"@[A-Za-z0-9_]{3,32}", " ", name)
    # 3) 推广群号：必须真的带「群 / 组」二字，或紧跟在广告词后的 @句柄。
    #    绝不允许把「资源」「发布」「首发」「电报」「TG」这类正常词单独吃掉。
    name = re.sub(r"(?:电报|TG|资源|发布|首发)(?:群|组)[@A-Za-z0-9_\-]*", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"唯一地址[@A-Za-z0-9_\-]*", " ", name)
    # 4) 网址 / 域名（整段匹配，不留 `.com` 残渣）
    name = _AD_URL_RE.sub(" ", name)
    # 5) 上一步可能留下孤立的 `.tld` 尾巴（如「合集 .cc」），单独清掉
    name = re.sub(r"(?:(?<=\s)|^)\.(?:com|net|org|xyz|top|cc|me|vip|tv|cn|io)(?=\s|$)", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[ \t]+", " ", name)
    name = re.sub(r"(?:[ \t]*[-—][ \t]*){2,}", " - ", name)
    name = name.strip(" ._-—#")

    # 安全网：清洗过度一律回退原名。改名丢文件，远比留个广告尾巴严重。
    if not name or len(name) < 2:
        name = original.strip(" ._-—#") or original
    return f"{name}{ext}" if ext else name


# 常见 MIME → 扩展名（后端未提供 fileName 时，用于把真实后缀补回来）
_MIME_EXT = {
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/mpeg": ".mpeg",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/flac": ".flac",
    "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf", "application/zip": ".zip",
}


def _ensure_archive_ext(name: str, local_path: str = "", mime: str = "", ftype: str = "") -> str:
    """确保归档文件名带扩展名。

    后端有时不返回 fileName，显示名会退化成「文件 <uniqueId>」（无后缀）。
    直接拿它归档，云端就会多出一个没有后缀的文件——播放器、Emby、Jellyfin
    全都认不出来，用户看到的就是「文件丢失了后缀名」。
    这里依次从 local_path / mimeType / type 推断真实后缀补回去。
    """
    nm = str(name or "").strip()
    if not nm:
        return nm
    if posixpath.splitext(nm)[1]:
        return nm  # 已有后缀，绝不改动
    ext = posixpath.splitext(str(local_path or ""))[1]
    if not ext:
        ext = _MIME_EXT.get(str(mime or "").strip().lower(), "")
    if not ext:
        ext = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg"}.get(str(ftype or "").strip().lower(), "")
    return f"{nm}{ext}" if ext else nm


def _same_file_name(a: str, b: str) -> bool:
    """判定两个文件名是否指向同一个文件（用于查重指纹）。

    只容忍「扩展名大小写不同」或「一方缺扩展名」这两种等价情况。

    绝不能用 `split(".")[0]` 比对——那会把
        `剧名.EP01.1080p.mp4`  与  `剧名.EP02.1080p.mp4`
    判成同一个文件，于是第二集在提交/归档时被当成重复**静默跳过**，
    用户真实丢文件。旧实现正是这么写的。
    """
    na = str(a or "").strip().lower()
    nb = str(b or "").strip().lower()
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, ea = posixpath.splitext(na)
    sb, eb = posixpath.splitext(nb)
    if not ea or not eb:
        # 一方没后缀：只允许主干完全相等（不是前缀、不是只比第一个点之前）
        return (sa or na) == (sb or nb)
    return sa == sb and ea == eb


def _human_to_bytes(size_str: str):
    if not size_str or not isinstance(size_str, str):
        return None
    s = size_str.strip().upper()
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for u, mul in sorted(units.items(), key=lambda x: -len(x[0])):
        if s.endswith(u):
            try:
                val = float(s[:-len(u)].strip())
                return int(val * mul)
            except (ValueError, TypeError):
                return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _sum_human(files: List[Dict[str, Any]]) -> str:
    total = 0
    has_any = False
    for f in files:
        b = f.get("size_bytes")
        if b is not None:
            try:
                total += int(b)
                has_any = True
            except (ValueError, TypeError):
                pass
    return _fmt_size(total) if has_any else "0 B"


def _classify_file_type(ftype: Any, mime: Any) -> str:
    t = str(ftype or "").strip().lower()
    if t in ("video", "animation"):
        return "video"
    if t == "photo":
        return "photo"
    if t == "audio":
        return "audio"
    m = str(mime or "").strip().lower()
    if m.startswith("video/"):
        return "video"
    if m.startswith("image/"):
        return "photo"
    if m.startswith("audio/"):
        return "audio"
    return "document"


def _match_size_bucket(size_bytes: Any, bucket: str) -> bool:
    if not bucket:
        return True
    if size_bytes is None:
        return False
    try:
        b = int(size_bytes)
    except (TypeError, ValueError):
        return False
    if bucket == "lt100mb":
        return 0 < b < 100 * 1024 ** 2
    if bucket == "100mb-500mb":
        return 100 * 1024 ** 2 <= b < 500 * 1024 ** 2
    if bucket == "gt1gb":
        return b >= 1024 ** 3
    return False


def _mask_secret(val: Any) -> str:
    s = str(val or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return s[:2] + "****" + s[-2:]


def _mask_token(tok: str) -> str:
    tok = str(tok or "").strip()
    if len(tok) > 10:
        return tok[:6] + "******" + tok[-4:]
    elif tok:
        return "******"
    return ""



def _extract_flood_wait_seconds(data: Any) -> Optional[int]:
    """智能从 TDLib 返回、HTTP 响应体或异常中提取限流秒数。"""
    if not data:
        return None
    if isinstance(data, dict):
        params = data.get("parameters")
        if isinstance(params, dict) and params.get("retry_after"):
            try:
                return int(params["retry_after"])
            except (ValueError, TypeError):
                pass
        code = data.get("code") or data.get("error_code")
        msg = str(data.get("message") or data.get("description") or "")
        m = FLOOD_WAIT_REGEX.search(msg)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
        if code in (420, 429):
            m = re.search(r"(\d+)", msg)
            if m:
                try:
                    return int(m.group(1))
                except (ValueError, TypeError):
                    pass
            return 30
    elif isinstance(data, (str, bytes)):
        s = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
        m = FLOOD_WAIT_REGEX.search(s)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
    elif isinstance(data, Exception):
        m = FLOOD_WAIT_REGEX.search(str(data))
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
    return None


ALLOWED_TG_DOMAINS = {"t.me", "telegram.me", "telegram.dog"}

RE_TG_PRIVATE = re.compile(
    r"^c/(\d{5,20})(?:/(\d+))?/(\d{1,12})$",
    re.IGNORECASE
)
RE_TG_PUBLIC = re.compile(
    r"^([a-zA-Z0-9_]{4,32})(?:/(\d+))?/(\d{1,12})$",
    re.IGNORECASE
)

_LINK_PATTERNS = [
    re.compile(r"t\.me/([A-Za-z0-9_]{4,32})/(\d{1,12})"),
    re.compile(r"t\.me/c/(\d{5,20})/(\d{1,12})"),
]


def _tg_err(e: Exception) -> str:
    """从 httpx.HTTPError / 后端错误体里提取可读信息。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            data = resp.json()
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or resp.status_code)
            if isinstance(err, str):
                return err
        except Exception:  # noqa: BLE001
            pass
        return f"后端返回 {resp.status_code}"
    msg = str(e).strip()
    return msg if msg else e.__class__.__name__


def _tg_err_public(e: Exception) -> str:
    """客户端安全版错误文案：不回显后端错误结构/内网 URL/部署拓扑。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        code = resp.status_code
        if code == 429:
            return "操作过于频繁，请稍后重试"
        if code in (401, 403):
            return "没有权限执行该操作，请重新登录后重试"
        if code >= 500:
            return "后端服务暂时不可用，请稍后重试"
        return "操作失败，请稍后重试"
    return "操作失败，请稍后重试"

