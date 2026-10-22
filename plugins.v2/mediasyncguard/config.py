"""MediaSyncGuard 插件配置模型（纯标准库，无 MoviePilot 依赖）。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "开启", "是"}:
            return True
        if text in {"0", "false", "no", "off", "关闭", "否", ""}:
            return False
    return default


def _as_int(value: Any, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_str_list(value: Any) -> List[str]:
    """把列表或逗号/换行分隔的字符串统一成去重后的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.extend(item.replace("\n", ",").split(","))
            elif item is not None:
                raw.append(str(item))
    else:
        raw = [str(value)]
    result: List[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _as_mapping_lines(value: Any) -> List[str]:
    """把路径映射配置统一成 ``源:目标`` 行列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = [str(value)]
    result: List[str] = []
    for item in raw:
        text = str(item).strip()
        if text and ":" in text and text not in result:
            result.append(text)
    return result


def parse_path_mappings(lines: Any) -> List[Tuple[str, str]]:
    """解析 ``源路径:目标路径`` 行列表（Windows 盘符等场景不支持，NAS 环境无需）。"""
    mappings: List[Tuple[str, str]] = []
    for line in _as_mapping_lines(lines):
        source, target = line.split(":", 1)
        source = source.strip().rstrip("/")
        target = target.strip().rstrip("/")
        if source and target:
            mappings.append((source, target))
    return mappings


def map_path(path: str, mappings: List[Tuple[str, str]]) -> str:
    """按完整路径段做前缀替换，避免 ``/media`` 误匹配 ``/media2``。"""
    if not path:
        return path
    text = str(path).replace("\\", "/").rstrip("/") or "/"
    for source, target in mappings:
        if text == source:
            return target
        if text.startswith(source + "/"):
            return target + text[len(source):]
    return text


@dataclass
class PluginConfig:
    """插件配置。

    默认值遵循“先安全、后自动化”的原则：插件启用后默认处于演练模式，
    自动删除开关全部关闭，只记录/通知候选项，由用户验证后手动开启。
    """

    # 基础
    enabled: bool = False
    notify: bool = True
    poll_seconds: int = 60
    downloaders: List[str] = field(default_factory=list)
    media_servers: List[str] = field(default_factory=list)
    media_path_map: List[str] = field(default_factory=lambda: ["/media:/medias"])
    download_path_map: List[str] = field(default_factory=list)

    # 自动化开关（默认全关，安全第一）
    dry_run: bool = True
    auto_media_to_torrent: bool = False
    auto_torrent_to_media: bool = False
    auto_delete_files: bool = True

    # 安全阈值
    grace_seconds: int = 600
    batch_limit_media: int = 10
    batch_limit_torrents: int = 3
    ignore_categories: List[str] = field(default_factory=lambda: ["KEEP"])
    ignore_tags: List[str] = field(default_factory=list)
    media_delete_roots: List[str] = field(default_factory=lambda: ["/medias"])
    download_delete_roots: List[str] = field(default_factory=lambda: ["/medias/Downloads"])

    # 清理行为
    delete_torrent_files: bool = True
    delete_media_sidecars: bool = True
    cleanup_empty_dirs: bool = True
    clean_moviepilot_history: bool = True
    scan_include_unfinished: bool = False
    keep_history_actions: int = 50

    @property
    def media_path_mappings(self) -> List[Tuple[str, str]]:
        return parse_path_mappings(self.media_path_map)

    @property
    def download_path_mappings(self) -> List[Tuple[str, str]]:
        return parse_path_mappings(self.download_path_map)

    @property
    def effective_poll_seconds(self) -> int:
        return _as_int(self.poll_seconds, 60, minimum=15, maximum=3600)

    @property
    def auto_enabled(self) -> bool:
        return bool(self.auto_media_to_torrent or self.auto_torrent_to_media)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PluginConfig":
        data = data or {}
        config = cls(
            enabled=_as_bool(data.get("enabled"), False),
            notify=_as_bool(data.get("notify"), True),
            poll_seconds=_as_int(data.get("poll_seconds"), 60, minimum=15, maximum=3600),
            downloaders=_as_str_list(data.get("downloaders")),
            media_servers=_as_str_list(data.get("media_servers")),
            media_path_map=_as_mapping_lines(data.get("media_path_map")) or ["/media:/medias"],
            download_path_map=_as_mapping_lines(data.get("download_path_map")),
            dry_run=_as_bool(data.get("dry_run"), True),
            auto_media_to_torrent=_as_bool(data.get("auto_media_to_torrent"), False),
            auto_torrent_to_media=_as_bool(data.get("auto_torrent_to_media"), False),
            auto_delete_files=_as_bool(data.get("auto_delete_files"), True),
            grace_seconds=_as_int(data.get("grace_seconds"), 600, minimum=0, maximum=86400),
            batch_limit_media=_as_int(data.get("batch_limit_media"), 10, minimum=0, maximum=100000),
            batch_limit_torrents=_as_int(data.get("batch_limit_torrents"), 3, minimum=0, maximum=100000),
            ignore_categories=_as_str_list(data.get("ignore_categories")) if data.get("ignore_categories") else ["KEEP"],
            ignore_tags=_as_str_list(data.get("ignore_tags")),
            media_delete_roots=_as_str_list(data.get("media_delete_roots")) or ["/medias"],
            download_delete_roots=_as_str_list(data.get("download_delete_roots")) or ["/medias/Downloads"],
            delete_torrent_files=_as_bool(data.get("delete_torrent_files"), True),
            delete_media_sidecars=_as_bool(data.get("delete_media_sidecars"), True),
            cleanup_empty_dirs=_as_bool(data.get("cleanup_empty_dirs"), True),
            clean_moviepilot_history=_as_bool(data.get("clean_moviepilot_history"), True),
            scan_include_unfinished=_as_bool(data.get("scan_include_unfinished"), False),
            keep_history_actions=_as_int(data.get("keep_history_actions"), 50, minimum=0, maximum=1000),
        )
        return config

    def to_dict(self) -> Dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def merged(self, **overrides: Any) -> "PluginConfig":
        """返回覆盖部分字段后的新配置对象。"""
        data = self.to_dict()
        data.update(overrides)
        return PluginConfig.from_dict(data)
