"""MediaSyncGuard 下载目录临时/残留文件扫描。

目标：找出下载目录中带“未完成文件后缀”（qBittorrent 的 ``.!qB``、``.!ut``、``.part`` 等）
的文件，并判断它们是否仍属于某个**现存**下载任务：

- 仍属于现存任务：说明是下载器未完成/校验中的文件，删除会破坏该任务 → 默认不可删除；
- 不属于任何现存任务：属于残留垃圾文件 → 可在扫描页勾选清理。

本模块只依赖标准库、``models`` 与 ``config``，文件读取由 :class:`FileSystemWalker` 完成，
下载任务文件清单通过 ``file_names_provider(downloader, hash) -> list[str]`` 回调提供，
便于在 MoviePilot 进程外做单元测试。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

try:  # MoviePilot 中作为包模块导入
    from .config import PluginConfig
    from .models import (
        TorrentItem,
        is_video_file,
        strip_download_temp_suffix,
    )
except ImportError:  # 单元测试/独立脚本
    from config import PluginConfig  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        TorrentItem,
        is_video_file,
        strip_download_temp_suffix,
    )

# 单次扫描最多遍历的文件数（防止下载目录异常巨大时拖垮扫描）
MAX_SCAN_FILES = 500_000
# 单次扫描最多输出的临时文件行数
MAX_ROWS = 500
# 单次扫描最多为多少个任务拉取文件清单（按 content_path 精确筛选后通常很少）
MAX_FILE_LIST_FETCH = 100

TEMP_SUFFIXES = (".!qb", ".!ut", ".part", ".td", ".tmp")


def _norm(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normpath(text)
    except (TypeError, ValueError):
        return text


def _under(path: str, root: str) -> bool:
    """判断 path 是否位于 root 内（按完整路径段）。"""
    target = _norm(path)
    base = _norm(root)
    if not target or not base:
        return False
    if target == base:
        return True
    return target.startswith(base + os.sep)


def _temp_suffix(name: str) -> str:
    lower = str(name or "").casefold()
    for suffix in TEMP_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return ""


@dataclass
class TempFileRow:
    """下载目录中一个带临时后缀的文件。"""

    id: str
    path: str
    name: str
    suffix: str
    size: int
    mtime: float
    canonical_path: str
    canonical_exists: bool
    owner_hashes: List[str] = field(default_factory=list)
    owner_names: List[str] = field(default_factory=list)
    owner_check_failed: bool = False

    @property
    def deletable(self) -> bool:
        """只有不属于任何现存任务、且校验没有失败时才允许清理。"""
        return not self.owner_hashes and not self.owner_check_failed

    @property
    def reason(self) -> str:
        if self.owner_hashes:
            return "owned_by_torrent"
        if self.owner_check_failed:
            return "owner_check_failed"
        return "orphan_temp_file"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "suffix": self.suffix,
            "size": int(self.size),
            "mtime": float(self.mtime),
            "canonical_path": self.canonical_path,
            "canonical_exists": bool(self.canonical_exists),
            "owner_hashes": list(self.owner_hashes),
            "owner_names": list(self.owner_names),
            "owner_check_failed": bool(self.owner_check_failed),
            "reason": self.reason,
            "deletable": self.deletable,
        }


@dataclass
class TempScanResult:
    rows: List[TempFileRow] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "stats": dict(self.stats),
            "errors": list(self.errors),
        }


def _iter_temp_files(roots: Iterable[str]) -> Iterable[Dict[str, Any]]:
    """遍历下载根目录，产出带临时后缀的普通文件信息。"""
    seen: set = set()
    scanned = 0
    for root in roots or []:
        base = _norm(root)
        if not base or not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
            for name in filenames:
                if scanned >= MAX_SCAN_FILES:
                    return
                suffix = _temp_suffix(name)
                if not suffix:
                    continue
                path = _norm(os.path.join(dirpath, name))
                if path in seen:
                    continue
                seen.add(path)
                scanned += 1
                try:
                    stat_result = os.stat(path)
                except OSError:
                    continue
                yield {
                    "path": path,
                    "name": name,
                    "suffix": suffix,
                    "size": int(getattr(stat_result, "st_size", 0) or 0),
                    "mtime": float(getattr(stat_result, "st_mtime", 0.0) or 0.0),
                }


def scan_temp_files(
    config: PluginConfig,
    torrents: Iterable[TorrentItem],
    file_names_provider: Optional[Callable[[str, str], Optional[List[str]]]],
    download_roots: Optional[Iterable[str]] = None,
) -> TempScanResult:
    """扫描下载目录中的临时后缀文件，并标注其归属的现存下载任务。"""
    result = TempScanResult()
    categories = getattr(config, "ignore_categories", None) or []
    del categories  # 临时文件扫描不受种子忽略规则影响，归属信息里保留即可
    roots = list(download_roots) if download_roots is not None else list(
        getattr(config, "download_delete_roots", None) or []
    )
    torrent_list = [item for item in (torrents or []) if isinstance(item, TorrentItem)]
    # 只保留有 content_path 的任务用于归属判断；用 content_path（而不是 save_path）筛选，
    # 避免把同一 save_path 下成千上万个无关任务都拉一遍文件清单。
    with_content: List[TorrentItem] = []
    for torrent in torrent_list:
        if _norm(torrent.content_path):
            with_content.append(torrent)
    file_cache: Dict[str, Optional[List[str]]] = {}
    fetched = 0

    def _get_names(torrent: TorrentItem) -> Optional[List[str]]:
        nonlocal fetched
        if not callable(file_names_provider):
            return None
        cache_key = f"{torrent.downloader}|{torrent.hash}"
        if cache_key in file_cache:
            return file_cache[cache_key]
        if fetched >= MAX_FILE_LIST_FETCH:
            file_cache[cache_key] = None
            return None
        fetched += 1
        try:
            names = file_names_provider(torrent.downloader, torrent.hash)
        except Exception as err:  # noqa: BLE001
            result.errors.append(f"读取任务文件清单失败（{cache_key}）：{err}")
            names = None
        file_cache[cache_key] = names
        return names

    for item in _iter_temp_files(roots):
        if len(result.rows) >= MAX_ROWS:
            result.errors.append(f"临时文件过多，仅展示前 {MAX_ROWS} 条")
            break
        path = item["path"]
        canonical_path = _norm(path[: -len(item["suffix"])])
        row = TempFileRow(
            id="temp:" + hashlib.sha1(path.encode("utf-8", "ignore")).hexdigest()[:16],
            path=path,
            name=item["name"],
            suffix=item["suffix"],
            size=item["size"],
            mtime=item["mtime"],
            canonical_path=canonical_path,
            canonical_exists=os.path.exists(canonical_path),
        )
        # 只检查 content_path 是 canonical_path 祖先/自身的现存任务（精确定位到目录或单文件）
        candidates = [
            torrent
            for torrent in with_content
            if _under(canonical_path, _norm(torrent.content_path))
        ]
        for torrent in candidates:
            names = _get_names(torrent)
            if names is None:
                row.owner_check_failed = True
                continue
            save_path = _norm(torrent.save_path)
            for name in names:
                full_path = _norm(os.path.join(save_path, str(name)))
                if full_path == canonical_path:
                    if torrent.hash not in row.owner_hashes:
                        row.owner_hashes.append(torrent.hash)
                        row.owner_names.append(torrent.name or torrent.hash)
                    break
        result.rows.append(row)

    owned = sum(1 for row in result.rows if row.owner_hashes)
    orphan = sum(1 for row in result.rows if row.deletable)
    failed = sum(1 for row in result.rows if row.owner_check_failed)
    result.stats = {
        "temp_files": len(result.rows),
        "owned_temp_files": owned,
        "orphan_temp_files": orphan,
        "check_failed_temp_files": failed,
        "file_lists_fetched": fetched,
    }
    return result
