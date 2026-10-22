"""MediaSyncGuard 的纯数据模型。

本模块只依赖 Python 标准库，便于在 MoviePilot 进程外进行单元测试。
禁止在此模块导入任何 ``app.*``（MoviePilot）模块。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 文件系统设备号 + inode 组成的硬链接唯一键
InodeKey = Tuple[int, int]

# 视为“媒体文件”的视频后缀（与 MoviePilot 默认媒体后缀保持一致，并去掉前导点）
VIDEO_EXTENSIONS = frozenset(
    {
        "mp4", "mkv", "ts", "m2ts", "mts", "iso", "rmvb", "rm", "avi", "mov",
        "wmv", "flv", "webm", "mpg", "mpeg", "m4v", "3gp", "divx", "vob", "f4v",
        "asf", "ogm", "ogv", "tp", "trp",
    }
)

# 媒体目录中与媒体文件同名、需要随媒体一起清理的附属文件后缀
SIDECAR_EXTENSIONS = frozenset(
    {
        "srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "smi",
        "nfo", "jpg", "jpeg", "png", "webp", "tbn", "bif", "gif",
        "txt", "md5", "sfv", "log", "cue", "xml",
    }
)

# 字幕类附属文件后缀（清理时优先匹配，便于统计）
SUBTITLE_EXTENSIONS = frozenset({"srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "smi"})

# 下载器“未完成文件”后缀。qBittorrent 启用 IncompleteFilesExt 时会把未完成文件
# 命名为 `xxx.mkv.!qB`，但该文件与媒体库文件可能仍是同一个 inode（硬链接）。
# 识别硬链接时必须先去掉该后缀，否则会把已关联的媒体误判为“无种子”。
DOWNLOAD_TEMP_SUFFIXES = (".!qb", ".!ut", ".part", ".td", ".tmp")


def strip_download_temp_suffix(name: str) -> str:
    """去掉下载器未完成文件后缀（不区分大小写）；非临时文件原样返回。"""
    text = str(name or "")
    lower = text.casefold()
    for suffix in DOWNLOAD_TEMP_SUFFIXES:
        if lower.endswith(suffix):
            return text[: -len(suffix)]
    return text


def file_extension(path: str) -> str:
    """返回小写、不带点的文件后缀；无后缀返回空字符串。

    对下载器未完成文件（如 ``movie.mkv.!qB``）返回其真实媒体后缀 ``mkv``。
    """
    if not path:
        return ""
    name = strip_download_temp_suffix(str(path).rsplit("/", 1)[-1])
    return os.path.splitext(name)[1].lower().lstrip(".")


def is_video_file(path: str) -> bool:
    """判断路径是否是视频文件。"""
    return file_extension(path) in VIDEO_EXTENSIONS


def is_subtitle_file(path: str) -> bool:
    """判断路径是否是字幕文件。"""
    return file_extension(path) in SUBTITLE_EXTENSIONS


def is_sidecar_file(path: str) -> bool:
    """判断路径是否是媒体附属文件（字幕、nfo、图片等）。"""
    return file_extension(path) in SIDECAR_EXTENSIONS


def safe_int(value: Any, default: int = 0) -> int:
    """把任意值安全转换为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """把任意值安全转换为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def media_key(server: str, item_id: str) -> str:
    """媒体条目的全局唯一键。"""
    return f"{server}|{item_id}"


def torrent_key(downloader: str, torrent_hash: str) -> str:
    """下载任务（种子）的全局唯一键。"""
    return f"{downloader}|{str(torrent_hash).lower()}"


def _parse_inode_key_list(value: Any) -> List[InodeKey]:
    """解析 ``[[dev, inode], ...]`` 形式的 inode 列表；非法值忽略。"""
    result: List[InodeKey] = []
    for item in value or []:
        try:
            dev, inode = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if dev and inode:
            result.append((dev, inode))
    return result


@dataclass
class MediaItem:
    """媒体服务器中的一个可播放媒体条目（电影或剧集）。"""

    server: str
    item_id: str
    path: str
    inode: int = 0
    dev: int = 0
    size: int = 0
    mtime: float = 0.0
    name: str = ""
    item_type: str = ""  # Movie / Episode（其它类型暂不处理）
    series_name: str = ""
    season: int = 0
    episode: int = 0
    parent_id: str = ""
    series_id: str = ""
    # 目录型媒体条目（蓝光 BDMV 等）内的多个视频文件 inode；
    # inode/dev 存主视频文件，其余放这里用于硬链接匹配。
    extra_inode_keys: List[InodeKey] = field(default_factory=list)

    @property
    def key(self) -> str:
        return media_key(self.server, self.item_id)

    @property
    def inode_key(self) -> InodeKey:
        return self.dev, self.inode

    @property
    def has_inode(self) -> bool:
        return bool(self.inode)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path) if self.path else ""

    @property
    def display_title(self) -> str:
        """用于界面与通知的展示标题。"""
        if self.item_type == "Episode" and self.series_name:
            parts = [self.series_name]
            if self.season:
                parts.append(f"S{self.season:02d}")
            if self.episode:
                parts.append(f"E{self.episode:02d}")
            return " ".join(parts)
        return self.name or os.path.basename(self.path) or self.item_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "item_id": self.item_id,
            "path": self.path,
            "inode": self.inode,
            "dev": self.dev,
            "size": self.size,
            "mtime": self.mtime,
            "name": self.name,
            "item_type": self.item_type,
            "series_name": self.series_name,
            "season": self.season,
            "episode": self.episode,
            "parent_id": self.parent_id,
            "series_id": self.series_id,
            "extra_inode_keys": [
                [int(dev), int(inode)] for dev, inode in (self.extra_inode_keys or [])
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        data = data or {}
        return cls(
            server=str(data.get("server") or ""),
            item_id=str(data.get("item_id") or ""),
            path=str(data.get("path") or ""),
            inode=safe_int(data.get("inode")),
            dev=safe_int(data.get("dev")),
            size=safe_int(data.get("size")),
            mtime=safe_float(data.get("mtime")),
            name=str(data.get("name") or ""),
            item_type=str(data.get("item_type") or ""),
            series_name=str(data.get("series_name") or ""),
            season=safe_int(data.get("season")),
            episode=safe_int(data.get("episode")),
            parent_id=str(data.get("parent_id") or ""),
            series_id=str(data.get("series_id") or ""),
            extra_inode_keys=_parse_inode_key_list(data.get("extra_inode_keys")),
        )


@dataclass
class TorrentItem:
    """下载器中的一个下载任务（种子）。"""

    downloader: str
    hash: str
    name: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)
    state: str = ""
    progress: float = 0.0
    size: int = 0
    save_path: str = ""
    content_path: str = ""
    added_on: int = 0
    completion_on: int = 0
    ratio: float = 0.0
    seeding_time: int = 0
    unsupported_path: bool = False
    path_exists: bool = True
    video_files: int = 0

    @property
    def key(self) -> str:
        return torrent_key(self.downloader, self.hash)

    @property
    def completed(self) -> bool:
        return self.progress >= 0.999

    @property
    def tag_list(self) -> List[str]:
        return [str(tag).strip() for tag in (self.tags or []) if str(tag).strip()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "downloader": self.downloader,
            "hash": self.hash,
            "name": self.name,
            "category": self.category,
            "tags": list(self.tags or []),
            "state": self.state,
            "progress": self.progress,
            "size": self.size,
            "save_path": self.save_path,
            "content_path": self.content_path,
            "added_on": self.added_on,
            "completion_on": self.completion_on,
            "ratio": self.ratio,
            "seeding_time": self.seeding_time,
            "unsupported_path": self.unsupported_path,
            "path_exists": self.path_exists,
            "video_files": self.video_files,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TorrentItem":
        data = data or {}
        return cls(
            downloader=str(data.get("downloader") or ""),
            hash=str(data.get("hash") or "").lower(),
            name=str(data.get("name") or ""),
            category=str(data.get("category") or ""),
            tags=[str(tag) for tag in (data.get("tags") or [])],
            state=str(data.get("state") or ""),
            progress=safe_float(data.get("progress")),
            size=safe_int(data.get("size")),
            save_path=str(data.get("save_path") or ""),
            content_path=str(data.get("content_path") or ""),
            added_on=safe_int(data.get("added_on")),
            completion_on=safe_int(data.get("completion_on")),
            ratio=safe_float(data.get("ratio")),
            seeding_time=safe_int(data.get("seeding_time")),
            unsupported_path=bool(data.get("unsupported_path")),
            path_exists=bool(data.get("path_exists", True)),
            video_files=safe_int(data.get("video_files")),
        )


@dataclass
class Snapshot:
    """某一时刻媒体库与下载器状态的快照。"""

    media: Dict[str, MediaItem] = field(default_factory=dict)
    torrents: Dict[str, TorrentItem] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def torrent_count(self) -> int:
        return len(self.torrents)

    @property
    def media_inode_keys(self) -> set:
        return {item.inode_key for item in self.media.values() if item.has_inode}

    @property
    def media_paths(self) -> set:
        return {item.path for item in self.media.values() if item.path}

    def inode_media_map(self) -> Dict[InodeKey, List[MediaItem]]:
        result: Dict[InodeKey, List[MediaItem]] = {}
        for item in self.media.values():
            if item.has_inode:
                result.setdefault(item.inode_key, []).append(item)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "media": {key: item.to_dict() for key, item in self.media.items()},
            "torrents": {key: item.to_dict() for key, item in self.torrents.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        data = data or {}
        return cls(
            media={
                str(key): MediaItem.from_dict(value)
                for key, value in (data.get("media") or {}).items()
            },
            torrents={
                str(key): TorrentItem.from_dict(value)
                for key, value in (data.get("torrents") or {}).items()
            },
            created_at=safe_float(data.get("created_at")),
        )


@dataclass(frozen=True)
class MediaRef:
    """指向媒体条目的轻量引用。"""

    server: str
    item_id: str

    @property
    def key(self) -> str:
        return media_key(self.server, self.item_id)

    def to_dict(self) -> Dict[str, str]:
        return {"server": self.server, "item_id": self.item_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaRef":
        data = data or {}
        return cls(server=str(data.get("server") or ""), item_id=str(data.get("item_id") or ""))


@dataclass(frozen=True)
class TorrentRef:
    """指向下载任务的轻量引用。"""

    downloader: str
    hash: str

    @property
    def key(self) -> str:
        return torrent_key(self.downloader, self.hash)

    def to_dict(self) -> Dict[str, str]:
        return {"downloader": self.downloader, "hash": self.hash}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TorrentRef":
        data = data or {}
        return cls(
            downloader=str(data.get("downloader") or ""),
            hash=str(data.get("hash") or "").lower(),
        )


@dataclass
class LinkIndex:
    """媒体条目与下载任务之间的硬链接/整理记录关系。

    - ``torrent_media``: 种子键 -> 关联媒体引用列表
    - ``media_torrents``: 媒体键 -> 关联种子引用列表
    - ``inode_media``: inode -> 当前媒体库中引用该 inode 的媒体列表
    - ``inode_torrents``: inode -> 当前下载器中引用该 inode 的种子列表
    - ``torrent_inodes``: 种子键 -> 该种子的视频文件 inode 列表
    - ``evidence``: 关系键 -> 关联依据（inode / history）
    """

    torrent_media: Dict[str, List[MediaRef]] = field(default_factory=dict)
    media_torrents: Dict[str, List[TorrentRef]] = field(default_factory=dict)
    inode_media: Dict[str, List[MediaRef]] = field(default_factory=dict)
    inode_torrents: Dict[str, List[TorrentRef]] = field(default_factory=dict)
    torrent_inodes: Dict[str, List[str]] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.torrent_media and not self.media_torrents

    @property
    def link_count(self) -> int:
        return sum(len(refs) for refs in self.torrent_media.values())

    def add_media_inode(self, inode_key: InodeKey, media: MediaItem) -> None:
        """登记媒体库中的 inode 引用。"""
        key = inode_key_str(inode_key)
        refs = self.inode_media.setdefault(key, [])
        ref = MediaRef(server=media.server, item_id=media.item_id)
        if ref not in refs:
            refs.append(ref)

    def add_torrent_inode(self, inode_key: InodeKey, torrent: TorrentItem) -> None:
        """登记下载器中某个视频文件 inode 的引用。"""
        key = inode_key_str(inode_key)
        refs = self.inode_torrents.setdefault(key, [])
        ref = TorrentRef(downloader=torrent.downloader, hash=torrent.hash)
        if ref not in refs:
            refs.append(ref)
        inodes = self.torrent_inodes.setdefault(torrent.key, [])
        if key not in inodes:
            inodes.append(key)

    def add_link(self, media: MediaItem, torrent: TorrentItem, evidence: str = "inode") -> None:
        """登记一条媒体 <-> 种子关联。"""
        pair_key = f"{media.key}->{torrent.key}"
        media_ref = MediaRef(server=media.server, item_id=media.item_id)
        torrent_ref = TorrentRef(downloader=torrent.downloader, hash=torrent.hash)

        refs = self.torrent_media.setdefault(torrent.key, [])
        if media_ref not in refs:
            refs.append(media_ref)
        refs = self.media_torrents.setdefault(media.key, [])
        if torrent_ref not in refs:
            refs.append(torrent_ref)
        # inode 关联的证据优先于整理记录
        if self.evidence.get(pair_key) != "inode":
            self.evidence[pair_key] = evidence

    def media_of(self, torrent: "TorrentItem | TorrentRef | str") -> List[MediaRef]:
        key = torrent if isinstance(torrent, str) else torrent.key
        return list(self.torrent_media.get(key, []))

    def torrents_of(self, media: "MediaItem | MediaRef | str") -> List[TorrentRef]:
        key = media if isinstance(media, str) else media.key
        return list(self.media_torrents.get(key, []))

    def current_torrents_of_inode(self, inode_key: InodeKey) -> List[TorrentRef]:
        """当前下载器中引用该 inode 的种子。"""
        return list(self.inode_torrents.get(inode_key_str(inode_key), []))

    def current_media_of_inode(self, inode_key: InodeKey) -> List[MediaRef]:
        """当前媒体库中引用该 inode 的媒体。"""
        return list(self.inode_media.get(inode_key_str(inode_key), []))

    def inodes_of_torrent(self, torrent: "TorrentItem | TorrentRef | str") -> List[str]:
        key = torrent if isinstance(torrent, str) else torrent.key
        return list(self.torrent_inodes.get(key, []))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "torrent_media": {
                key: [ref.to_dict() for ref in refs] for key, refs in self.torrent_media.items()
            },
            "media_torrents": {
                key: [ref.to_dict() for ref in refs] for key, refs in self.media_torrents.items()
            },
            "inode_media": {
                key: [ref.to_dict() for ref in refs] for key, refs in self.inode_media.items()
            },
            "inode_torrents": {
                key: [ref.to_dict() for ref in refs] for key, refs in self.inode_torrents.items()
            },
            "torrent_inodes": {key: list(inodes) for key, inodes in self.torrent_inodes.items()},
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LinkIndex":
        data = data or {}
        return cls(
            torrent_media={
                str(key): [MediaRef.from_dict(ref) for ref in (refs or [])]
                for key, refs in (data.get("torrent_media") or {}).items()
            },
            media_torrents={
                str(key): [TorrentRef.from_dict(ref) for ref in (refs or [])]
                for key, refs in (data.get("media_torrents") or {}).items()
            },
            inode_media={
                str(key): [MediaRef.from_dict(ref) for ref in (refs or [])]
                for key, refs in (data.get("inode_media") or {}).items()
            },
            inode_torrents={
                str(key): [TorrentRef.from_dict(ref) for ref in (refs or [])]
                for key, refs in (data.get("inode_torrents") or {}).items()
            },
            torrent_inodes={
                str(key): [str(inode) for inode in (inodes or [])]
                for key, inodes in (data.get("torrent_inodes") or {}).items()
            },
            evidence={str(key): str(value) for key, value in (data.get("evidence") or {}).items()},
        )


def inode_key_str(inode_key: InodeKey) -> str:
    """把 (dev, inode) 元组编码成可稳定持久化的字符串。"""
    try:
        dev, inode = inode_key
    except (TypeError, ValueError):
        return "0:0"
    return f"{int(dev)}:{int(inode)}"


def parse_inode_key(text: str) -> InodeKey:
    """把 ``dev:inode`` 字符串还原为元组。"""
    try:
        dev, inode = str(text).split(":", 1)
        return int(dev), int(inode)
    except (TypeError, ValueError):
        return 0, 0


@dataclass
class PendingAction:
    """延迟执行（等待宽限期）的删除动作。"""

    action_id: str
    kind: str  # delete_torrents / delete_media
    created_at: float
    due_at: float
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    status: str = "pending"  # pending / done / failed / skipped / cancelled
    results: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "due_at": self.due_at,
            "reason": self.reason,
            "payload": dict(self.payload or {}),
            "dry_run": self.dry_run,
            "status": self.status,
            "results": list(self.results or []),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingAction":
        data = data or {}
        return cls(
            action_id=str(data.get("action_id") or ""),
            kind=str(data.get("kind") or ""),
            created_at=safe_float(data.get("created_at")),
            due_at=safe_float(data.get("due_at")),
            reason=str(data.get("reason") or ""),
            payload=dict(data.get("payload") or {}),
            dry_run=bool(data.get("dry_run")),
            status=str(data.get("status") or "pending"),
            results=list(data.get("results") or []),
        )
