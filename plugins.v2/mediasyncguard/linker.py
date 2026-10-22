"""MediaSyncGuard 硬链接索引构建模块。

本模块只依赖 Python 标准库与本插件的 ``models.py``；**禁止**导入任何 MoviePilot
（``app.*``）模块。所有文件系统操作都是只读的（``os.stat`` / ``os.scandir``），
不会创建、修改或删除真实文件，便于单元测试。

核心思路：媒体库中的媒体文件与下载器中的种子文件如果是同一个 inode（硬链接），
就认为“该种子生成了该媒体条目”。索引同时登记“媒体 -> inode -> 种子”的双向关系，
供 guard 做最保守的级联删除决策。
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # MoviePilot 中作为包模块导入：mediasyncguard.linker
    from .models import InodeKey, LinkIndex, MediaItem, TorrentItem, is_video_file, torrent_key
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from models import InodeKey, LinkIndex, MediaItem, TorrentItem, is_video_file, torrent_key

logger = logging.getLogger(__name__)


def normalize_history_path(path: Any) -> str:
    """把整理记录/媒体路径规范化为 ``normpath`` 形式，空值返回空字符串。"""
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        return os.path.normpath(text)
    except (TypeError, ValueError):
        return text


@dataclass
class InodeInfo:
    """一次 ``os.stat`` 得到的文件身份信息。"""

    path: str
    inode: int
    dev: int
    size: int
    mtime: float

    @property
    def inode_key(self) -> InodeKey:
        return self.dev, self.inode


@dataclass
class LinkBuildStats:
    """硬链接索引构建过程的统计信息（用于扫描页展示/排障）。"""

    media_total: int = 0
    media_with_inode: int = 0
    torrents_total: int = 0
    torrents_walked: int = 0
    torrents_unsupported: int = 0
    torrents_missing_path: int = 0
    video_files: int = 0
    walk_errors: int = 0
    linked_pairs: int = 0
    history_linked_pairs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换成契约 §5.2 的 ``stats`` 键，方便 runtime 直接合并。"""
        return {
            "media_total": int(self.media_total),
            "torrent_total": int(self.torrents_total),
            "link_pairs": int(self.linked_pairs),
            "torrents_walked": int(self.torrents_walked),
            "torrents_unsupported": int(self.torrents_unsupported),
            "torrents_missing_path": int(self.torrents_missing_path),
            "video_files": int(self.video_files),
            "walk_errors": int(self.walk_errors),
        }


class FileSystemWalker:
    """只读文件系统遍历器。

    - :meth:`stat`：单文件 ``os.stat``（跟随文件符号链接），失败返回 ``None``；
    - :meth:`collect_video_inodes`：收集文件/目录下的视频文件 inode。目录遍历不跟随
      目录符号链接（防环），文件符号链接按真实 inode 计入；按 ``realpath`` 缓存结果，
      使 cross-seed 共享同一目录时只遍历一次。

    遍历错误（权限、文件消失等）只累加 :attr:`walk_errors`，不抛异常。
    """

    def __init__(self, max_files_per_torrent: int = 200000, max_depth: int = 16):
        try:
            cap = int(max_files_per_torrent)
        except (TypeError, ValueError):
            cap = 200000
        try:
            depth = int(max_depth)
        except (TypeError, ValueError):
            depth = 16
        self.max_files_per_torrent = max(1, cap)
        self.max_depth = max(0, depth)
        self.walk_errors = 0
        self._cache: Dict[str, Tuple[InodeKey, ...]] = {}

    # ------------------------------------------------------------------ 缓存
    @property
    def cache_size(self) -> int:
        """已缓存的 realpath 数量。"""
        return len(self._cache)

    def reset_cache(self) -> None:
        """清除遍历缓存（每次全量扫描建议使用全新 walker 或调用本方法）。"""
        self._cache.clear()

    # ------------------------------------------------------------------ 基础
    def stat(self, path: str) -> Optional[InodeInfo]:
        """``os.stat`` 包装；失败/空路径返回 ``None``，不抛异常。"""
        if not path:
            return None
        try:
            st = os.stat(str(path))
        except (OSError, ValueError):
            return None
        try:
            return InodeInfo(
                path=str(path),
                inode=int(st.st_ino),
                dev=int(st.st_dev),
                size=int(st.st_size),
                mtime=float(st.st_mtime),
            )
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - 防御性
            return None

    @staticmethod
    def _inode_key(st: os.stat_result) -> InodeKey:
        return int(st.st_dev), int(st.st_ino)

    # ------------------------------------------------------------------ 遍历
    def collect_video_inodes(self, path: str) -> List[InodeKey]:
        """收集 ``path``（文件或目录）下的视频文件 inode 列表。

        - ``path`` 是文件：直接 ``os.stat``（按契约，单文件种子即其内容文件）；
        - ``path`` 是目录：递归 ``scandir`` 只收集视频文件，不跟随目录符号链接；
        - 同一 ``realpath`` 的结果会被缓存，cross-seed 共享目录只遍历一次；
        - 权限错误/不存在返回 ``[]``，绝不抛异常。
        """
        if not path:
            return []
        try:
            real_path = os.path.realpath(str(path))
        except (OSError, ValueError):
            real_path = str(path)
        cached = self._cache.get(real_path)
        if cached is not None:
            return list(cached)
        try:
            inodes = self._collect_uncached(str(path))
        except Exception as exc:  # noqa: BLE001 - 遍历器绝不能让扫描线程崩溃
            self.walk_errors += 1
            logger.warning("遍历路径失败（%s）：%s", path, exc)
            inodes = []
        # 去重且保持顺序
        deduped: List[InodeKey] = []
        for key in inodes:
            if key not in deduped:
                deduped.append(key)
        self._cache[real_path] = tuple(deduped)
        return list(deduped)

    def _collect_uncached(self, path: str) -> List[InodeKey]:
        try:
            root_stat = os.stat(path)
        except (OSError, ValueError):
            self.walk_errors += 1
            return []
        if stat_module.S_ISREG(root_stat.st_mode):
            # 单文件种子内容也必须是视频文件；避免把 .exe 等伪装文件当成媒体
            if not is_video_file(path):
                return []
            return [self._inode_key(root_stat)]
        if not stat_module.S_ISDIR(root_stat.st_mode):
            return []

        inodes: List[InodeKey] = []
        visited_files = 0
        stack: List[Tuple[str, int]] = [(path, 0)]
        truncated = False

        while stack:
            directory, depth = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                self.walk_errors += 1
                continue
            for entry in entries:
                if visited_files >= self.max_files_per_torrent:
                    truncated = True
                    break
                try:
                    # 目录符号链接不跟随（防环）；普通子目录按深度限制入栈
                    if entry.is_dir(follow_symlinks=False):
                        if depth < self.max_depth:
                            stack.append((entry.path, depth + 1))
                        continue
                    if entry.is_symlink():
                        # 文件符号链接允许指向真实 inode；指向目录的符号链接跳过
                        try:
                            target_stat = os.stat(entry.path)
                        except OSError:
                            self.walk_errors += 1
                            continue
                        if stat_module.S_ISDIR(target_stat.st_mode):
                            continue
                        visited_files += 1
                        if stat_module.S_ISREG(target_stat.st_mode) and is_video_file(entry.path):
                            inodes.append(self._inode_key(target_stat))
                        continue
                    try:
                        file_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        self.walk_errors += 1
                        continue
                    visited_files += 1
                    if stat_module.S_ISREG(file_stat.st_mode) and is_video_file(entry.path):
                        inodes.append(self._inode_key(file_stat))
                except OSError:
                    self.walk_errors += 1
                    continue
            if truncated:
                break

        if truncated:
            logger.warning(
                "单个种子的文件数超过上限 %s，已截断遍历：%s",
                self.max_files_per_torrent,
                path,
            )
        return inodes


@dataclass
class LinkBuildResult:
    """索引构建结果。"""

    index: LinkIndex = field(default_factory=LinkIndex)
    stats: LinkBuildStats = field(default_factory=LinkBuildStats)


class LinkBuilder:
    """把媒体条目与下载任务按 inode（或整理记录兜底）关联成 :class:`LinkIndex`。"""

    def __init__(self, walker: Optional[FileSystemWalker] = None):
        self._walker = walker or FileSystemWalker()

    @property
    def walker(self) -> FileSystemWalker:
        return self._walker

    def build(
        self,
        media_items: List[MediaItem],
        torrents: List[TorrentItem],
        history_rows: Optional[Iterable[Tuple[str, str, str]]] = None,
    ) -> LinkBuildResult:
        """构建硬链接索引。

        流程（契约 §3.2）：

        1. 媒体条目 inode 为 0 时用 walker 补全（就地更新 item 的 inode/dev/size/mtime）；
        2. 每个媒体登记 ``inode_media``；
        3. 每个种子：unsupported -> 跳过统计；content_path 空 -> missing 统计；
           否则收集视频 inode，登记 ``inode_torrents`` / ``torrent_inodes``，
           并对同一 inode 上已登记的媒体建立 ``inode`` 关联；
        4. ``history_rows``（downloader, torrent_hash, media_path）作为兜底：
           dest 路径与媒体 path（normpath 后）相等且种子 key 存在时建立 ``history`` 关联；
        5. 填充统计信息。
        """
        items: List[MediaItem] = [item for item in (media_items or []) if item is not None]
        torrent_items: List[TorrentItem] = [
            torrent for torrent in (torrents or []) if torrent is not None
        ]
        stats = LinkBuildStats(media_total=len(items), torrents_total=len(torrent_items))
        index = LinkIndex()
        walker = self._walker
        walk_errors_before = walker.walk_errors

        # 1/2. 媒体补全 inode 并登记 inode_media
        media_by_key: Dict[str, MediaItem] = {}
        for item in items:
            if not item.has_inode:
                info = walker.stat(item.path)
                if info is not None:
                    item.inode = info.inode
                    item.dev = info.dev
                    item.size = info.size
                    item.mtime = info.mtime
            media_by_key[item.key] = item
            media_inode_keys: List[InodeKey] = []
            if item.has_inode:
                media_inode_keys.append(item.inode_key)
            for extra_key in getattr(item, "extra_inode_keys", None) or []:
                try:
                    key = (int(extra_key[0]), int(extra_key[1]))
                except (TypeError, ValueError, IndexError):
                    continue
                if key[0] and key[1] and key not in media_inode_keys:
                    media_inode_keys.append(key)
            if media_inode_keys:
                stats.media_with_inode += 1
                for media_inode_key in media_inode_keys:
                    index.add_media_inode(media_inode_key, item)

        # 3. 种子视频文件 inode
        torrent_by_key: Dict[str, TorrentItem] = {torrent.key: torrent for torrent in torrent_items}
        # 历史兜底只在“种子仍存在于本次索引输入”时建立，避免把不存在的种子写进索引
        history_torrents: Dict[str, TorrentItem] = {}
        for key, torrent in torrent_by_key.items():
            if torrent.unsupported_path:
                stats.torrents_unsupported += 1
                continue
            content_path = str(torrent.content_path or "").strip()
            if not content_path:
                stats.torrents_missing_path += 1
                continue
            stats.torrents_walked += 1
            inodes = walker.collect_video_inodes(content_path)
            # 以实际遍历结果回填，保证快照/扫描展示与索引一致
            torrent.video_files = len(inodes)
            stats.video_files += len(inodes)
            for inode_key in inodes:
                index.add_torrent_inode(inode_key, torrent)
                for media_ref in index.current_media_of_inode(inode_key):
                    media_item = media_by_key.get(media_ref.key)
                    if media_item is not None:
                        index.add_link(media_item, torrent, evidence="inode")
            # 历史兜底候选：所有实际存在（非 unsupported）的种子都可参与
            history_torrents[key] = torrent
        # unsupported 的种子也允许作为历史兜底目标（key 存在即可），
        # 但它们的文件不可见，无法校验 inode，因此只在媒体索引中保留关联证据。
        for key, torrent in torrent_by_key.items():
            history_torrents.setdefault(key, torrent)

        # 4. history 兜底关联
        if history_rows:
            media_by_path: Dict[str, MediaItem] = {}
            for item in items:
                path_key = normalize_history_path(item.path)
                if path_key and path_key not in media_by_path:
                    media_by_path[path_key] = item
            # src -> 种子候选：MoviePilot 历史记录有可能缺少 download_hash，
            # 此时用整理来源路径反查仍存在的种子（目录型种子匹配其内部文件）。
            torrents_by_source: Dict[str, List[TorrentItem]] = {}
            for torrent in history_torrents.values():
                content_path = normalize_history_path(torrent.content_path)
                if content_path:
                    torrents_by_source.setdefault(content_path, []).append(torrent)
            for row in history_rows:
                if row is None:
                    continue
                try:
                    row_values = tuple(row)
                except TypeError:
                    continue
                if len(row_values) >= 4:
                    downloader, torrent_hash, media_path, source_path = row_values[:4]
                elif len(row_values) == 3:
                    downloader, torrent_hash, media_path = row_values
                    source_path = ""
                else:
                    continue
                if not media_path or (not downloader and not torrent_hash and not source_path):
                    continue
                media_item = media_by_path.get(normalize_history_path(media_path))
                if media_item is None:
                    continue
                matched: List[TorrentItem] = []
                if torrent_hash:
                    if downloader:
                        torrent = history_torrents.get(
                            torrent_key(str(downloader), str(torrent_hash))
                        )
                        if torrent is not None:
                            matched.append(torrent)
                    else:
                        target_hash = str(torrent_hash).lower()
                        matched.extend(
                            torrent
                            for torrent in history_torrents.values()
                            if str(torrent.hash).lower() == target_hash
                        )
                elif source_path:
                    normalized_source = normalize_history_path(source_path)
                    exact = torrents_by_source.get(normalized_source) or []
                    matched.extend(exact)
                    if not exact:
                        for torrent in history_torrents.values():
                            content_path = normalize_history_path(torrent.content_path)
                            if content_path and normalized_source.startswith(content_path + os.sep):
                                matched.append(torrent)
                for torrent in matched:
                    index.add_link(media_item, torrent, evidence="history")

        # 5. 统计关联数（以最终 evidence 为准：inode 关联优先于 history）
        linked_pairs = 0
        history_linked_pairs = 0
        for evidence in index.evidence.values():
            if evidence == "inode":
                linked_pairs += 1
            elif evidence == "history":
                history_linked_pairs += 1
        stats.linked_pairs = linked_pairs
        stats.history_linked_pairs = history_linked_pairs
        stats.walk_errors = max(0, walker.walk_errors - walk_errors_before)
        return LinkBuildResult(index=index, stats=stats)

