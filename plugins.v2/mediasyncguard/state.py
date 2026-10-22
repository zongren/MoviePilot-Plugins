"""MediaSyncGuard 状态持久化模块。

只依赖 Python 标准库与本插件的 ``models.py``；**禁止**导入任何 MoviePilot（``app.*``）
模块，便于在 MoviePilot 进程外进行单元测试。

状态文件固定为 ``<data_dir>/state.json.gz``（gzip 压缩的 UTF-8 JSON），
保存时先写同目录 ``*.tmp`` 再 ``os.replace`` 原子替换，避免进程中断写坏状态。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:  # MoviePilot 中作为包模块导入：mediasyncguard.state
    from .models import LinkIndex, MediaItem, PendingAction, Snapshot, TorrentItem
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from models import LinkIndex, MediaItem, PendingAction, Snapshot, TorrentItem

logger = logging.getLogger(__name__)

# 状态数据版本；结构不兼容变化时递增，旧版本数据会被忽略（返回空状态）。
DATA_VERSION = 1

STATE_FILENAME = "state.json.gz"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """把任意值安全转换为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意值安全转换为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_path(path: Any) -> str:
    """把路径规范化为 ``normpath`` 形式，空值返回空字符串。"""
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
class SnapshotDiff:
    """两次轮询快照之间的差异。"""

    lost_media: List[MediaItem] = field(default_factory=list)
    lost_torrents: List[TorrentItem] = field(default_factory=list)
    new_media_count: int = 0
    new_torrent_count: int = 0

    @property
    def is_empty(self) -> bool:
        """是否没有任何变化。"""
        return not (
            self.lost_media
            or self.lost_torrents
            or self.new_media_count
            or self.new_torrent_count
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lost_media": [item.to_dict() for item in self.lost_media],
            "lost_torrents": [item.to_dict() for item in self.lost_torrents],
            "new_media_count": self.new_media_count,
            "new_torrent_count": self.new_torrent_count,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SnapshotDiff":
        data = data if isinstance(data, dict) else {}
        diff = cls()
        for raw in data.get("lost_media") or []:
            if isinstance(raw, dict):
                diff.lost_media.append(MediaItem.from_dict(raw))
        for raw in data.get("lost_torrents") or []:
            if isinstance(raw, dict):
                diff.lost_torrents.append(TorrentItem.from_dict(raw))
        diff.new_media_count = _safe_int(data.get("new_media_count"))
        diff.new_torrent_count = _safe_int(data.get("new_torrent_count"))
        return diff


def diff_snapshots(previous: Optional[Snapshot], current: Snapshot) -> SnapshotDiff:
    """对比两次快照，得出消失的媒体/种子。

    防误判规则（必须，宁可漏判也不误判）：

    - 只有 ``previous`` 中某媒体 key 不在 ``current.media`` 中、
      **且** 其 ``inode_key`` 不在 ``current.media_inode_keys`` 中、
      **且** 其 ``path`` 不在 ``current.media_paths`` 中时，才算 ``lost_media``。
      这样媒体库重扫导致 key 变化、只改名、软链接换了路径等情况都不会误判。
    - ``previous`` 为 ``None``（首次运行，无基线）时返回空 diff，绝不产生删除候选。
    - ``lost_torrents``：``previous`` 中 key 不在 ``current.torrents`` 的种子。
    """
    diff = SnapshotDiff()
    if previous is None or current is None:
        # 没有基线快照时不能做任何“消失”判断
        return diff

    current_media_keys = set(current.media.keys())
    current_inode_keys = current.media_inode_keys
    current_paths = current.media_paths
    # 额外用规范化路径做一次兜底保护（例如历史快照里带 "./" 之类的写法）
    current_normalized_paths = {normalize_path(path) for path in current_paths if path}

    for key, item in previous.media.items():
        if key in current_media_keys:
            continue
        if item.has_inode and item.inode_key in current_inode_keys:
            # inode 仍存在：媒体库重扫/改名/换 key，不算删除
            continue
        if item.path and item.path in current_paths:
            continue
        if item.path and normalize_path(item.path) in current_normalized_paths:
            continue
        diff.lost_media.append(item)

    for key, torrent in previous.torrents.items():
        if key not in current.torrents:
            diff.lost_torrents.append(torrent)

    diff.new_media_count = len(set(current.media.keys()) - set(previous.media.keys()))
    diff.new_torrent_count = len(set(current.torrents.keys()) - set(previous.torrents.keys()))
    return diff


@dataclass
class PersistedState:
    """插件需要持久化的全部状态。"""

    snapshot: Snapshot = field(default_factory=Snapshot)
    links: LinkIndex = field(default_factory=LinkIndex)
    pending: List[PendingAction] = field(default_factory=list)
    last_poll_at: float = 0.0
    last_scan_summary: Dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    version: int = DATA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        snapshot = self.snapshot if isinstance(self.snapshot, Snapshot) else Snapshot()
        links = self.links if isinstance(self.links, LinkIndex) else LinkIndex()
        pending: List[Dict[str, Any]] = []
        for action in self.pending or []:
            if isinstance(action, PendingAction):
                pending.append(action.to_dict())
        summary = self.last_scan_summary
        return {
            "version": DATA_VERSION,
            "snapshot": snapshot.to_dict(),
            "links": links.to_dict(),
            "pending": pending,
            "last_poll_at": _safe_float(self.last_poll_at),
            "last_scan_summary": dict(summary) if isinstance(summary, dict) else {},
            "last_error": str(self.last_error or ""),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PersistedState":
        data = data if isinstance(data, dict) else {}
        pending: List[PendingAction] = []
        for raw in data.get("pending") or []:
            if isinstance(raw, dict):
                try:
                    pending.append(PendingAction.from_dict(raw))
                except Exception:  # noqa: BLE001 - 单条历史动作损坏不应影响整体加载
                    logger.debug("忽略损坏的待执行动作记录", exc_info=True)
        summary = data.get("last_scan_summary")
        snapshot_data = data.get("snapshot")
        links_data = data.get("links")
        return cls(
            snapshot=Snapshot.from_dict(snapshot_data if isinstance(snapshot_data, dict) else {}),
            links=LinkIndex.from_dict(links_data if isinstance(links_data, dict) else {}),
            pending=pending,
            last_poll_at=_safe_float(data.get("last_poll_at")),
            last_scan_summary=dict(summary) if isinstance(summary, dict) else {},
            last_error=str(data.get("last_error") or ""),
            version=_safe_int(data.get("version"), DATA_VERSION) or DATA_VERSION,
        )


class StateStore:
    """``data_dir/state.json.gz`` 的读写封装。

    - :meth:`load` 在文件不存在、gzip/JSON 损坏、版本不兼容时返回空状态，不抛异常；
    - :meth:`save` 采用“先写临时文件再原子替换”，失败只记日志不抛异常；
    - 对 ``pending`` 列表不做长度限制（由 runtime 决定保留策略）。
    """

    def __init__(self, data_dir: Union[str, Path]):
        self._data_dir = Path(data_dir) if data_dir is not None else Path(".")
        self._last_error = ""

    @property
    def data_dir(self) -> Path:
        """状态目录。"""
        return self._data_dir

    @property
    def path(self) -> Path:
        """状态文件路径。"""
        return self._data_dir / STATE_FILENAME

    @property
    def last_error(self) -> str:
        """最近一次读写失败的错误摘要（成功时为空）。"""
        return self._last_error

    @property
    def exists(self) -> bool:
        """状态文件是否存在。"""
        try:
            return self.path.is_file()
        except OSError:
            return False

    def load(self) -> PersistedState:
        """读取状态；任何异常都返回空 ``PersistedState``。"""
        self._last_error = ""
        path = self.path
        try:
            if not path.is_file():
                return PersistedState()
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("状态数据不是 JSON 对象")
            version = _safe_int(data.get("version"), 0)
            if version != DATA_VERSION:
                logger.warning(
                    "状态文件版本不兼容（文件版本 %s，当前版本 %s），已忽略旧状态",
                    version,
                    DATA_VERSION,
                )
                return PersistedState()
            return PersistedState.from_dict(data)
        except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError, EOFError) as exc:
            self._last_error = f"读取状态失败：{exc}"
            logger.warning("读取状态文件失败，将使用空状态：%s", exc)
            return PersistedState()
        except Exception as exc:  # noqa: BLE001 - 持久化层绝不能让主流程崩溃
            self._last_error = f"读取状态失败：{exc}"
            logger.warning("读取状态文件出现未预期错误，将使用空状态：%s", exc, exc_info=True)
            return PersistedState()

    def save(self, state: PersistedState) -> None:
        """保存状态（gzip JSON，原子替换）；失败只记日志不抛异常。"""
        self._last_error = ""
        tmp_path: Optional[Path] = None
        try:
            data = state.to_dict() if isinstance(state, PersistedState) else PersistedState().to_dict()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_name(self.path.name + ".tmp")
            with gzip.open(tmp_path, "wt", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self.path)
        except Exception as exc:  # noqa: BLE001 - 持久化层绝不能让主流程崩溃
            self._last_error = f"保存状态失败：{exc}"
            logger.error("保存状态文件失败：%s", exc)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("清理状态临时文件失败", exc_info=True)

    def clear(self) -> None:
        """删除状态文件（以及可能残留的临时文件）；失败只记日志。"""
        for target in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除状态文件失败（%s）：%s", target, exc)
