"""MediaSyncGuard 媒体文件清理（纯标准库 + 本插件 models/config）。

本模块只负责“规划”和“执行”媒体删除后的文件清理：

- ``plan_media_cleanup``：只读扫描媒体所在目录，生成待删附属文件（字幕/nfo/图片等）
  与待删空目录计划，**不会删除任何文件**；
- ``apply_media_cleanup``：按计划先删文件、再自底向上删除空目录，所有异常都会被
  记录而不是抛出；
- ``is_safe_delete_target`` / ``is_path_under``：文件删除安全阀，确保只清理
  ``media_delete_roots`` 内、且不在 ``download_delete_roots`` 内的路径。

本模块禁止导入任何 ``app.*``（MoviePilot）模块，便于在临时目录中做单元测试。
"""

from __future__ import annotations

import errno
import logging
import os
from typing import Any, Dict, List, Optional

try:  # MoviePilot 中作为包模块导入：mediasyncguard.cleanup
    from .config import PluginConfig
    from .models import SIDECAR_EXTENSIONS, MediaItem, is_video_file
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from config import PluginConfig
    from models import SIDECAR_EXTENSIONS, MediaItem, is_video_file

logger = logging.getLogger(__name__)


def _norm(path: Any) -> str:
    """把路径规范化为 ``normpath`` 字符串；空值返回空字符串。"""
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        return os.path.normpath(text)
    except (TypeError, ValueError):
        return text


def _norm_roots(roots: Optional[List[str]]) -> List[str]:
    """规范化根目录列表（去空、去重、保留顺序）。"""
    result: List[str] = []
    for root in roots or []:
        text = _norm(root)
        if text and text not in result:
            result.append(text)
    return result


def is_path_under(path: str, roots: List[str]) -> bool:
    """判断 ``path`` 是否位于任意一个 ``roots`` 目录内（含目录本身）。

    按完整路径段比较，避免 ``/media`` 误匹配 ``/media2``。
    """
    target = _norm(path)
    if not target:
        return False
    for root in _norm_roots(roots):
        if root == os.sep:
            # 根目录（/）包含所有绝对路径
            return target.startswith(os.sep)
        if target == root or target.startswith(root + os.sep):
            return True
    return False


def _is_same_or_parent(path: str, root: str) -> bool:
    """判断 ``path`` 是否等于 ``root`` 或 ``root`` 的父目录。"""
    target = _norm(path)
    ancestor = _norm(root)
    if not target or not ancestor:
        return False
    if target == ancestor:
        return True
    return ancestor.startswith(target + os.sep)


def is_safe_delete_target(
    path: str,
    config: PluginConfig,
    media_roots: Optional[List[str]] = None,
) -> bool:
    """文件/目录删除安全阀。

    返回 True 需同时满足：

    - 路径位于 ``media_delete_roots``（或显式传入的 ``media_roots``）内；
    - 路径**不**位于 ``download_delete_roots`` 内（下载器管理的文件不能在这里删）；
    - 路径不是任何根目录本身，也不是任何根目录的父目录。
    """
    target = _norm(path)
    if not target:
        return False
    roots = _norm_roots(media_roots if media_roots is not None else config.media_delete_roots)
    if not roots or not is_path_under(target, roots):
        return False
    download_roots = _norm_roots(config.download_delete_roots)
    if download_roots and is_path_under(target, download_roots):
        return False
    for root in roots:
        if _is_same_or_parent(target, root):
            return False
    for root in download_roots:
        if _is_same_or_parent(target, root):
            return False
    return True


def _same_path(left: str, right: str) -> bool:
    """规范化后比较两个路径是否相同。"""
    return bool(left) and bool(right) and _norm(left) == _norm(right)


def plan_media_cleanup(
    media: MediaItem,
    config: PluginConfig,
    protected_video_paths: Optional[set] = None,
) -> Dict[str, Any]:
    """规划一次媒体删除后的附属文件/空目录清理（不实际删除）。

    返回 ``{"sidecar_files": [...], "remove_dirs": [...], "skipped": [...]}``：

    - 仅当 ``media.path`` 位于 ``config.media_delete_roots`` 内才规划；
    - Movie：同目录下所有 ``SIDECAR_EXTENSIONS`` 文件进入 ``sidecar_files``；
      若目录内已无其它视频文件（``protected_video_paths`` 中的路径也视为“其它文件”），
      目录进入 ``remove_dirs``；
    - Episode：只清理与视频文件同名的附属文件（stem 相同），目录本身不删；
    - ``config.delete_media_sidecars=False`` → ``sidecar_files`` 为空；
    - ``config.cleanup_empty_dirs=False`` → ``remove_dirs`` 为空。

    ``protected_video_paths`` 中的路径永远不会被本函数列入删除计划，仅用于判断
    “目录是否可以安全删除”。
    """
    plan: Dict[str, Any] = {"sidecar_files": [], "remove_dirs": [], "skipped": []}
    skipped: List[str] = plan["skipped"]

    media_path = _norm(getattr(media, "path", ""))
    if not media_path:
        skipped.append("媒体路径为空，跳过文件清理")
        return plan
    if not is_path_under(media_path, config.media_delete_roots):
        skipped.append("媒体路径不在 media_delete_roots 内，跳过文件清理")
        return plan

    folder = os.path.dirname(media_path)
    if not folder:
        skipped.append("无法确定媒体所在目录，跳过文件清理")
        return plan

    protected = {_norm(item) for item in (protected_video_paths or set()) if _norm(item)}

    try:
        entries = list(os.scandir(folder))
    except OSError as err:
        skipped.append(f"读取媒体目录失败：{err}")
        return plan

    media_stem = os.path.splitext(media_path)[0]
    item_type = str(getattr(media, "item_type", "") or "")

    sidecar_files: List[str] = []
    other_videos: List[str] = []
    leftovers: List[str] = []
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=True):
                # 子目录/特殊文件一律视为“目录不空”，不规划删目录
                leftovers.append(entry.path)
                continue
        except OSError:
            leftovers.append(entry.path)
            continue
        entry_path = _norm(entry.path)
        if _same_path(entry_path, media_path):
            continue
        name = entry.name or os.path.basename(entry_path)
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ext in SIDECAR_EXTENSIONS:
            if item_type == "Episode":
                # 剧集只清理与视频同名的附属文件
                if os.path.splitext(entry_path)[0] != media_stem:
                    leftovers.append(entry_path)
                    continue
            sidecar_files.append(entry_path)
            continue
        if is_video_file(name) or ext == "strm":
            # .strm 虽非视频实体，但代表同目录下的另一媒体条目，视为“目录不空”
            other_videos.append(entry_path)
            continue
        # 名字像视频但目前无法识别的文件 / 其它文件：保守视为目录不空
        leftovers.append(entry_path)

    if not config.delete_media_sidecars:
        sidecar_files = []

    # 目录内仍有其它视频（多版本/多集共用目录）时，只清理与当前媒体同名的附属文件，
    # 避免误删其它版本共用的 folder 级海报/NFO/字幕。
    if item_type == "Movie" and other_videos:
        sidecar_files = [
            path for path in sidecar_files if os.path.splitext(path)[0] == media_stem
        ]

    remove_dirs: List[str] = []
    # 只有 Movie 会在删除媒体文件后尝试清理空目录；剧集目录通常还有其它集
    if item_type == "Movie" and config.cleanup_empty_dirs:
        protected_inside = [
            item for item in protected if item and os.path.dirname(item) == folder and item != media_path
        ]
        if other_videos or protected_inside or leftovers:
            # 目录内还有其它视频或其它文件，删除目录不安全
            pass
        else:
            remove_dirs.append(folder)

    plan["sidecar_files"] = sorted(sidecar_files)
    plan["remove_dirs"] = sorted(remove_dirs)
    return plan


def apply_media_cleanup(plan: Dict[str, Any]) -> Dict[str, Any]:
    """按计划删除附属文件与空目录。

    执行顺序：先删文件，再自底向上删除目录（只删空目录）。任何异常都会记录到
    ``failed``，不会向调用方抛出。返回::

        {"deleted": [...], "failed": [{"path": ..., "error": ...}], "removed_dirs": [...]}
    """
    result: Dict[str, Any] = {"deleted": [], "failed": [], "removed_dirs": []}
    if not isinstance(plan, dict):
        return result

    failed: List[Dict[str, str]] = result["failed"]

    for raw_path in plan.get("sidecar_files") or []:
        path = _norm(raw_path)
        if not path:
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            # 文件已经不存在：视为无需处理
            continue
        except IsADirectoryError:
            failed.append({"path": path, "error": "目标是目录，拒绝删除"})
        except OSError as err:
            failed.append({"path": path, "error": str(err)})
        else:
            result["deleted"].append(path)

    # 自底向上：路径越深越先删；同深度按字典序保证稳定
    directories = sorted(
        {_norm(item) for item in (plan.get("remove_dirs") or []) if _norm(item)},
        key=lambda item: (item.count(os.sep), len(item)),
        reverse=True,
    )
    for directory in directories:
        try:
            os.rmdir(directory)
        except FileNotFoundError:
            continue
        except OSError as err:
            if err.errno in (errno.ENOTEMPTY, errno.EEXIST):
                # 目录里出现了新文件：保守跳过
                continue
            failed.append({"path": directory, "error": str(err)})
        else:
            result["removed_dirs"].append(directory)

    return result
