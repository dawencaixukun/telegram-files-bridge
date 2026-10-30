# -*- coding: utf-8 -*-
"""
services/disk_service.py — 本地磁盘占用的「目录级明细」服务
============================================================
仪表盘 KPI「本地磁盘占用」过去只有一个分区级数字（shutil.disk_usage），
用户只能看到「27 GB」，看不出这 27 GB 到底被什么占了 —— 是归档下载残留、
TDLib 媒体缓存、系统日志，还是 docker 镜像。本模块按目录把占用拆开，
并支持逐级下钻。

统计口径（与 du 一致，**不是** ls 的 apparent size）：
  * 用 st_blocks * 512（实际分配的磁盘块），不用 st_size。稀疏文件、
    4 KiB 块尾、以及已被删除但仍被进程打开的文件，两种口径能差出 GB 级；
    实测本机 apparent 27.99 GB / allocated 27.66 GB / df used 26.99 GB，
    只有 allocated 口径才和 df 对得上；
  * 硬链接按 (st_dev, st_ino) 去重，同一个 inode 只计一次；
  * 不跟随符号链接；不跨设备（否则 /proc、/sys、网络挂载都会被算进来，
    总量与 df 对不上）；
  * 「自身占用」= 子树总量 - 各子目录之和：直接躺在该目录下的散文件
    （例如 /swapfile）也会单独成行，不会被吞掉。

一次完整扫描（根分区 271k 个条目）实测约 2.3 秒，所以结果带 TTL 缓存，
扫描跑在线程里，不阻塞事件循环。

所有模块内私有名统一加 _disk_ 前缀：services/__init__.py 会把本模块的
全部公开名星号导出，_norm/_CACHE 这类通用名会污染全局命名空间。
"""
import os
import stat as _stat
import time
import shutil
import threading
from typing import Any, Dict, List, Tuple

from core.config import APP_ROOT_DIR, BASE_DIR
from core.logging import log


# 单次扫描的时间预算：宁可标记 partial（并把数字当「下限」告知用户），
# 也不把 CPU 占满让页面卡死。
_DISK_SCAN_BUDGET = 30.0
# 目录聚合的最大深度（相对扫描根）。更深的文件归到最近的已登记祖先上，
# 继续下钻时以该目录为新根重扫。
_DISK_TREE_MAX_DEPTH = 4
# 缓存 TTL 与条目上限：用户连续点几层都在缓存里，过期重扫。
_DISK_CACHE_TTL = 120.0
_DISK_CACHE_MAX = 16

# 伪文件系统：即便同设备也一律不扫（读它们毫无意义且可能卡住）。
_DISK_PSEUDO_FS = frozenset({"/proc", "/sys", "/dev", "/run"})

_DISK_SCAN_CACHE: Dict[str, Dict[str, Any]] = {}
_DISK_CACHE_LOCK = threading.Lock()
_DISK_SCAN_LOCK = threading.Lock()


def _disk_norm(path: str) -> str:
    """归一化到绝对路径；空值落到数据目录。"""
    p = str(path or "").strip()
    if not p:
        p = APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR
    try:
        return os.path.abspath(p)
    except Exception:  # noqa: BLE001
        return str(p or "/")


def _disk_mount_point(path: str) -> str:
    """向上找到 path 所在文件系统的挂载点（st_dev 变化的那一层）。"""
    cur = _disk_norm(path)
    try:
        dev = os.stat(cur).st_dev
    except OSError:
        return "/"
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return cur
        try:
            if os.stat(parent).st_dev != dev:
                return cur
        except OSError:
            return cur
        cur = parent


def _disk_rel_depth(root: str, path: str) -> int:
    """path 相对 root 的层数（root 自身为 0，越界返回 99）。"""
    if path == root:
        return 0
    try:
        rel = os.path.relpath(path, root)
    except Exception:  # noqa: BLE001
        return 99
    if rel == "." or rel.startswith(".."):
        return 99
    return rel.count(os.sep) + 1


def _disk_scan_subtree(root: str, deadline: float) -> Dict[str, Any]:
    """扫描 root 子树，返回按目录聚合的「分配字节数」。

    {"total", "own", "subtree", "files", "top_files", "partial", "skipped", "error"?}
      total     —— 子树内普通文件分配字节之和（硬链接去重后）
      own       —— 目录绝对路径 → **本目录直属文件**的字节（不含子目录）
      subtree   —— 目录绝对路径 → 含全部子目录在内的字节
      files     —— 目录绝对路径 → 含子目录在内的文件个数
      top_files —— 直接躺在 root 下的散文件名 → 字节（/swapfile 这类）
      partial   —— 是否因超时提前退出（结果不完整，调用方必须如实告知）

    关键点：必须区分 own 与 subtree。只登记 own 的话，像 /var 这种
    「自己没文件、东西全在子目录里」的目录会显示 0.00 GB —— 用户点开
    分区一看全是大片 0，等于这个功能没做。subtree 由 own 向上回卷得到。
    """
    own: Dict[str, int] = {}
    own_files: Dict[str, int] = {}
    top_files: Dict[str, int] = {}
    seen_inodes = set()
    total = 0
    partial = False
    skipped = 0
    try:
        dev = os.stat(root).st_dev
    except OSError as e:  # noqa: BLE001
        return {"total": 0, "own": own, "subtree": {}, "files": {}, "top_files": {},
                "partial": True, "skipped": 0, "error": str(e)}

    own[root] = 0
    own_files[root] = 0
    # 栈元素：(当前目录, 最近一个已登记的祖先目录)
    stack: List[Tuple[str, str]] = [(root, root)]
    while stack:
        if time.monotonic() > deadline:
            partial = True
            break
        cur, acc = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            skipped += 1
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    continue
                mode = st.st_mode
                if _stat.S_ISDIR(mode):
                    if st.st_dev != dev:
                        # 跨设备（含 /proc、/sys、网络挂载）不参与统计，
                        # 否则「目录合计」会超过 df 的整个分区容量。
                        skipped += 1
                        continue
                    child_acc = acc
                    if _disk_rel_depth(root, entry.path) <= _DISK_TREE_MAX_DEPTH:
                        if entry.path not in own:
                            own[entry.path] = 0
                            own_files[entry.path] = 0
                        child_acc = entry.path
                    stack.append((entry.path, child_acc))
                    continue
                if not _stat.S_ISREG(mode):
                    continue  # 管道/套接字/设备文件不占普通磁盘块
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_inodes:
                        continue  # 硬链接去重：同一 inode 只算一次
                    seen_inodes.add(key)
                size = int(getattr(st, "st_blocks", 0) or 0) * 512
                total += size
                own[acc] = own.get(acc, 0) + size
                own_files[acc] = own_files.get(acc, 0) + 1
                # 直接躺在扫描根下的散文件单独记账：否则 /swapfile 这种
                # 4 GB 的单文件会被并进「目录自身占用」，用户看不到它叫什么。
                if os.path.dirname(entry.path) == root:
                    top_files[entry.path] = top_files.get(entry.path, 0) + size

    # own → subtree：每个已登记目录把自己的字节向上回卷给所有祖先。
    # 祖先必然也已登记（深度逐层 +1），所以一次遍历就能得到闭包。
    subtree: Dict[str, int] = {k: 0 for k in own}
    files: Dict[str, int] = {k: 0 for k in own}
    for path, size in own.items():
        cnt = own_files.get(path, 0)
        cur = path
        while True:
            if cur in subtree:
                subtree[cur] += size
                files[cur] += cnt
            if cur == root:
                break
            parent = os.path.dirname(cur)
            if parent == cur or _disk_rel_depth(root, parent) > _DISK_TREE_MAX_DEPTH:
                # 父目录未登记（不该发生，防御性兜底：直接记到 root 上）
                subtree[root] += size
                files[root] += cnt
                break
            cur = parent
    return {"total": total, "own": own, "subtree": subtree, "files": files,
            "top_files": top_files, "partial": partial, "skipped": skipped}


def _disk_scan_cached(path: str, max_age: float = _DISK_CACHE_TTL) -> Dict[str, Any]:
    """带 TTL 缓存的扫描；同一路径并发请求只真正扫一次。"""
    with _DISK_CACHE_LOCK:
        hit = _DISK_SCAN_CACHE.get(path)
        if hit and (time.monotonic() - hit["ts"]) < max_age:
            data = dict(hit["data"])
            data["cached"] = True
            data["ageSeconds"] = int(time.monotonic() - hit["ts"])
            return data
    with _DISK_SCAN_LOCK:
        # 双检：等锁期间可能已经有人扫完写进缓存了
        with _DISK_CACHE_LOCK:
            hit = _DISK_SCAN_CACHE.get(path)
            if hit and (time.monotonic() - hit["ts"]) < max_age:
                data = dict(hit["data"])
                data["cached"] = True
                data["ageSeconds"] = int(time.monotonic() - hit["ts"])
                return data
        started = time.monotonic()
        raw = _disk_scan_subtree(path, started + _DISK_SCAN_BUDGET)
        finished = time.monotonic()
        raw["ts"] = finished
        raw["elapsedMs"] = int((finished - started) * 1000)
        with _DISK_CACHE_LOCK:
            _DISK_SCAN_CACHE[path] = {"ts": raw["ts"], "data": raw}
            if len(_DISK_SCAN_CACHE) > _DISK_CACHE_MAX:
                oldest = min(_DISK_SCAN_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
                _DISK_SCAN_CACHE.pop(oldest, None)
        out = dict(raw)
        out["cached"] = False
        out["ageSeconds"] = 0
        return out


def _disk_known_note(path: str) -> str:
    """给几个用户最需要一眼认出的目录加说明（不解释纯系统目录）。"""
    data_root = os.path.abspath(APP_ROOT_DIR)
    notes = {
        data_root: "应用数据根（任务库 / 归档日志 / TDLib 登录态）",
        os.path.join(data_root, "account"): "TDLib 账号缓存（db.sqlite + 媒体文件）",
        os.path.join(data_root, "downloads"): "本系统下载落地目录",
        os.path.join(data_root, "logs"): "运行日志",
        os.path.join(os.path.dirname(data_root), "session-backups"): "Session 冷备包",
        os.path.join(os.path.dirname(data_root), "app"): "程序本体（源码 + venv）",
        "/var/log": "系统日志（journald / syslog）",
        "/var/lib": "容器镜像、包管理缓存等系统库数据",
        "/var/lib/docker": "Docker 镜像与运行时",
        "/var/lib/containerd": "containerd 镜像层",
        "/usr": "系统程序与运行库",
        "/opt": "第三方应用本体",
        "/tmp": "临时文件",
        "/swapfile": "交换文件",
    }
    return notes.get(path, "")


def disk_breakdown(path: str = "", max_entries: int = 40) -> Dict[str, Any]:
    """返回指定目录下一层的占用明细（按大小降序）。

    path 为空时以数据目录所在分区的挂载点为起点，而不是数据目录本身。

      path / parent       —— 当前位置与上一级（已到根则为空）
      total               —— 子树内普通文件分配字节之和
      subtreeTotal        —— 本目录含全部子目录的字节数（= total）
      selfBytes           —— 总量里归不到任何已列出子项的部分（正常为 0）
      used / free / fsTotal —— 该路径所在分区的真实读数（shutil.disk_usage）
      residualBytes       —— 仅在分区根有意义：分区已用 - 本层合计
                             （系统保留块、已删除但仍被进程占用的文件等）
      entries             —— [{name, path, dir, bytes, files, pct, note}]
      partial             —— 扫描是否被超时截断（true 时数字是下限）
    """
    # 空路径的默认起点必须是**整个分区**，不是数据目录：用户点开这个弹窗
    # 想知道的是「28 GB 都去哪了」，只给他看 app-data 会答非所问。
    if str(path or "").strip():
        target = _disk_norm(path)
    else:
        target = _disk_mount_point(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
    if not os.path.isdir(target):
        return {"ok": False, "message": f"目录不存在或不可访问：{target}"}
    if target in _DISK_PSEUDO_FS:
        return {"ok": False, "message": "系统伪文件系统不参与占用统计"}

    scan = _disk_scan_cached(target)
    # subtree 是「含全部子目录」的字节数 —— 只有它才能回答「这个目录占多少」。
    # 早期版本误用了「本目录直属文件」的口径，/var、/usr 这种东西全在
    # 子目录里的目录会显示 0.00 GB，点开一看全是 0。
    subtree: Dict[str, int] = scan.get("subtree") or {}
    files: Dict[str, int] = scan.get("files") or {}
    total = int(scan["total"])

    entries: List[Dict[str, Any]] = []
    children_bytes = 0
    top_files: Dict[str, int] = scan.get("top_files") or {}
    for p, b in subtree.items():
        if p == target:
            continue
        if os.path.dirname(p) != target:
            continue  # 只取直接子目录；更深的靠下钻
        children_bytes += b
        entries.append({
            "name": os.path.basename(p),
            "path": p,
            "dir": True,
            "bytes": int(b),
            "files": int(files.get(p, 0)),
            "note": _disk_known_note(p),
        })
    # 直接躺在该目录下的散文件（/swapfile 这类）也必须成行，否则 4 GB
    # 的单文件会静默并进「自身占用」，用户永远看不出它是什么。
    _top_sum = 0
    for fp, fb in top_files.items():
        _top_sum += int(fb)
        entries.append({
            "name": os.path.basename(fp),
            "path": fp,
            "dir": False,
            "bytes": int(fb),
            "files": 1,
            "note": _disk_known_note(fp),
        })
    entries.sort(key=lambda e: e["bytes"], reverse=True)
    truncated = max(0, len(entries) - int(max_entries))
    entries = entries[:max_entries]

    # 「自身占用」= 总量里既不是直接子目录、也没有单独成行的余量。
    # 正常情况应为 0；扫描被截断（partial）或存在不可读目录时会大于 0，
    # 这时的正确语义是「知道它存在但归不到具体子项」，不能悄悄丢掉。
    self_bytes = max(0, total - children_bytes - _top_sum)

    fs_total = used = free = 0
    try:
        u = shutil.disk_usage(target)
        fs_total, used, free = int(u.total), int(u.used), int(u.free)
    except Exception as e:  # noqa: BLE001
        log.debug("磁盘读数失败(%s): %s", target, e)

    # residual 只在分区根有意义：它是「分区已用 - 本目录合计」的差额。
    # 对子目录算这个数是没有意义的（/var 的合计 10.46 GB 与整个分区的
    # 28 GB 相减只会得到一堆噪音），所以只保留在分区根。
    is_mount_root = target == _disk_mount_point(target)
    residual = max(0, used - total) if is_mount_root else 0

    for e in entries:
        e["pct"] = round(e["bytes"] / total * 100.0, 1) if total else 0.0

    return {
        "ok": True,
        "path": target,
        "parent": os.path.dirname(target) if os.path.dirname(target) != target else "",
        "isMountRoot": is_mount_root,
        "total": total,
        "subtreeTotal": int(subtree.get(target, total)),
        "selfBytes": self_bytes,
        "residualBytes": residual,
        "used": used,
        "free": free,
        "fsTotal": fs_total,
        "entries": entries,
        "truncated": truncated,
        "partial": bool(scan.get("partial")),
        "skipped": int(scan.get("skipped", 0) or 0),
        "elapsedMs": int(scan.get("elapsedMs", 0) or 0),
        "cached": bool(scan.get("cached")),
        "ageSeconds": int(scan.get("ageSeconds", 0) or 0),
        "scannedAt": time.strftime("%H:%M:%S"),
    }


def disk_kpi_hint() -> Dict[str, Any]:
    """仪表盘 KPI 卡片用的轻量读数（不做目录扫描，避免页面渲染被拖慢）。"""
    root = _disk_mount_point(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
    info: Dict[str, Any] = {"root": root, "used": 0, "total": 0, "free": 0, "pct": 0.0}
    try:
        u = shutil.disk_usage(root)
        info.update({
            "used": int(u.used),
            "total": int(u.total),
            "free": int(u.free),
            "pct": round(u.used / u.total * 100.0, 1) if u.total else 0.0,
        })
    except Exception as e:  # noqa: BLE001
        log.debug("磁盘读数失败: %s", e)
    return info


def disk_cache_clear() -> int:
    """清空扫描缓存（手动刷新用），返回清掉的数量。"""
    with _DISK_CACHE_LOCK:
        n = len(_DISK_SCAN_CACHE)
        _DISK_SCAN_CACHE.clear()
    return n
