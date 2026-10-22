"""MoviePilot 服务适配层：Jellyfin REST 客户端与 qBittorrent 客户端封装。

本模块把 MoviePilot 的服务发现（``DownloaderHelper`` / ``MediaServerHelper``）与
具体的 Jellyfin REST、qBittorrent 客户端包装成 MediaSyncGuard 运行时可用的统一接口。

设计原则：

- 只做“读取/最小化写操作”（Jellyfin 删除条目、qB 删除种子），不做任何缓存以外的
  副作用；所有网络/实例异常都转换成 :class:`ClientError` 或 ``(False, 原因)``，
  由 runtime 决定如何降级；
- Jellyfin 媒体路径按插件配置 ``media_path_map`` 从 Jellyfin 容器路径映射到
  MoviePilot 可见路径后再 ``os.stat`` 取 inode；
- qB 下载器路径优先使用服务模块的 ``normalize_return_path``（已处理路径映射），
  失败时退回原始路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.log import logger

try:  # MoviePilot 中作为包模块导入：mediasyncguard.clients
    from .config import PluginConfig, map_path
    from .models import MediaItem, TorrentItem, safe_float, safe_int
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from config import PluginConfig, map_path
    from models import MediaItem, TorrentItem, safe_float, safe_int

# Jellyfin 分页参数
JELLYFIN_PAGE_SIZE = 5000
JELLYFIN_MAX_PAGES = 500
# 单次拉取最多记录多少条路径告警，避免刷屏
MAX_PATH_WARNINGS = 50

# 本插件当前支持的下载器/媒体服务器类型（小写）
SUPPORTED_DOWNLOADER_TYPES = frozenset({"qbittorrent"})
SUPPORTED_MEDIA_SERVER_TYPES = frozenset({"jellyfin"})


class ClientError(Exception):
    """客户端构建或数据抓取失败（runtime 据此做“失败降级”，绝不当成空数据）。"""


@dataclass
class DownloaderOption:
    """配置页可选的下载器。"""

    name: str
    type: str
    default: bool = False
    enabled: bool = True
    path_mappings: List[Any] = field(default_factory=list)


@dataclass
class MediaServerOption:
    """配置页可选的媒体服务器。"""

    name: str
    type: str
    enabled: bool = True
    host: str = ""


@dataclass
class ClientBundle:
    """一次配置构建出的全部客户端集合。"""

    jellyfin: List["JellyfinApi"] = field(default_factory=list)
    qbittorrent: List["QbittorrentApi"] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # 配置期望使用但本次未能成功构建的服务名（用于运行时判断“覆盖不完整”，
    # 覆盖不完整时禁止自动删除，避免复核逻辑因缺少数据源而变成“空判断”）
    missing_jellyfin: List[str] = field(default_factory=list)
    missing_qbittorrent: List[str] = field(default_factory=list)
    selected_jellyfin: List[str] = field(default_factory=list)
    selected_qbittorrent: List[str] = field(default_factory=list)
    enabled_qbittorrent: List[str] = field(default_factory=list)


@dataclass
class ServiceSelection:
    """根据插件配置解析出的服务选择结果（供运行时做覆盖校验）。"""

    downloaders: List[str] = field(default_factory=list)
    media_servers: List[str] = field(default_factory=list)
    enabled_downloaders: List[str] = field(default_factory=list)
    enabled_media_servers: List[str] = field(default_factory=list)
    # 所有已配置（含临时禁用）的受支持服务：用于识别“未纳管/被禁用但仍在跑”的下载器，
    # 这类实例的数据目录可能不可见，必须按不可校验处理。
    configured_downloaders: List[str] = field(default_factory=list)
    configured_media_servers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    """安全转换为去空白字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _value_of(raw: Any, key: str, default: Any = None) -> Any:
    """从 dict-like（qbittorrentapi 的 TorrentDictionary 等）对象取字段。"""
    if raw is None:
        return default
    try:
        if isinstance(raw, dict):
            return raw.get(key, default)
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                return getter(key)
        return getattr(raw, key, default)
    except Exception:  # noqa: BLE001 - 三方对象字段读取失败按缺失处理
        return default


def _split_tags(value: Any) -> List[str]:
    """把 qB 的逗号分隔标签字符串/列表统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = str(value).split(",")
    tags: List[str] = []
    for item in raw:
        text = _text(item)
        if text and text not in tags:
            tags.append(text)
    return tags


def _normalize_host(host: str) -> str:
    """规范化 Jellyfin 地址：补协议、去掉尾部斜杠后统一以 ``/`` 结尾。"""
    text = _text(host)
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "http://" + text
    return text.rstrip("/") + "/"


def _configs_as_dict(configs: Any) -> Dict[str, Any]:
    """把 ``get_configs()`` 的 dict/list 返回值统一成 ``name -> conf``。"""
    if isinstance(configs, dict):
        return {str(name): conf for name, conf in configs.items() if name and conf}
    result: Dict[str, Any] = {}
    for conf in configs or []:
        name = _text(getattr(conf, "name", None))
        if name:
            result[name] = conf
    return result


def _close_response(response: Any) -> None:
    """尽力关闭 HTTP 响应，避免连接泄漏。"""
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _downloader_helper() -> Any:
    """延迟导入并创建 MoviePilot ``DownloaderHelper``（便于单元测试替换）。"""
    from app.helper.downloader import DownloaderHelper

    return DownloaderHelper()


def _mediaserver_helper() -> Any:
    """延迟导入并创建 MoviePilot ``MediaServerHelper``（便于单元测试替换）。"""
    from app.helper.mediaserver import MediaServerHelper

    return MediaServerHelper()


def _normalizer_from_module(module: Any) -> Callable[[Any, str], str]:
    """基于媒体服务器/下载器模块的 ``normalize_return_path`` 构造路径映射函数。"""
    func = getattr(module, "normalize_return_path", None)
    if not callable(func):
        def _identity(raw_path: Any, downloader: str) -> str:
            return _text(raw_path)

        return _identity

    def _normalize(raw_path: Any, downloader: str) -> str:
        if raw_path is None or _text(raw_path) == "":
            return ""
        try:
            result = func(Path(str(raw_path)), downloader)
            text = _text(result)
            if text:
                return text
        except Exception as err:  # noqa: BLE001 - 映射失败退回原始路径
            logger.debug(f"路径反向映射失败（{downloader}）：{raw_path}，{err}")
        return str(raw_path)

    return _normalize


# ---------------------------------------------------------------------------
# 服务发现
# ---------------------------------------------------------------------------
class ServiceCatalog:
    """列出 MoviePilot 中已配置、且本插件支持的下载器/媒体服务器。"""

    def list_downloaders(self) -> List[DownloaderOption]:
        """只返回类型为 qBittorrent 的已启用下载器配置。"""
        try:
            helper = _downloader_helper()
            configs = _configs_as_dict(helper.get_configs())
        except Exception as err:  # noqa: BLE001 - 配置页不能因读取失败崩溃
            logger.error(f"读取 MoviePilot 下载器配置失败：{err}")
            return []
        options: List[DownloaderOption] = []
        for name, conf in configs.items():
            try:
                conf_type = _text(getattr(conf, "type", ""))
                if conf_type.lower() not in SUPPORTED_DOWNLOADER_TYPES:
                    continue
                if getattr(conf, "enabled", True) is False:
                    continue
                options.append(
                    DownloaderOption(
                        name=name,
                        type=conf_type,
                        default=bool(getattr(conf, "default", False)),
                        enabled=True,
                        path_mappings=list(getattr(conf, "path_mapping", None) or []),
                    )
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(f"读取下载器「{name}」配置失败：{err}")
        return options

    def list_media_servers(self) -> List[MediaServerOption]:
        """只返回类型为 Jellyfin 的已启用媒体服务器配置。"""
        try:
            helper = _mediaserver_helper()
            configs = _configs_as_dict(helper.get_configs())
        except Exception as err:  # noqa: BLE001
            logger.error(f"读取 MoviePilot 媒体服务器配置失败：{err}")
            return []
        options: List[MediaServerOption] = []
        for name, conf in configs.items():
            try:
                conf_type = _text(getattr(conf, "type", ""))
                if conf_type.lower() not in SUPPORTED_MEDIA_SERVER_TYPES:
                    continue
                if getattr(conf, "enabled", True) is False:
                    continue
                data = getattr(conf, "config", None)
                data = data if isinstance(data, dict) else {}
                options.append(
                    MediaServerOption(
                        name=name,
                        type=conf_type,
                        enabled=True,
                        host=_text(data.get("host")),
                    )
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(f"读取媒体服务器「{name}」配置失败：{err}")
        return options


# ---------------------------------------------------------------------------
# Jellyfin REST 客户端
# ---------------------------------------------------------------------------
class JellyfinApi:
    """Jellyfin 12.x REST API 的最小封装（列表 + 删除条目）。"""

    def __init__(
        self,
        name: str,
        host: str,
        apikey: str,
        path_mappings: List[Tuple[str, str]],
        timeout: int = 30,
        page_size: int = JELLYFIN_PAGE_SIZE,
    ):
        self.name = _text(name)
        self.host = _normalize_host(host)
        self.apikey = _text(apikey)
        self.path_mappings = list(path_mappings or [])
        self.timeout = safe_int(timeout, 30) or 30
        self.page_size = max(1, safe_int(page_size, JELLYFIN_PAGE_SIZE) or JELLYFIN_PAGE_SIZE)

    @property
    def configured(self) -> bool:
        """host 与 apikey 是否齐全。"""
        return bool(self.host and self.apikey)

    def _request_utils(self) -> Any:
        """构造带 Jellyfin 鉴权头的 RequestUtils。"""
        from app.utils.http import RequestUtils

        headers = {
            "Authorization": f'MediaBrowser Token="{self.apikey}"',
            "Accept": "application/json",
        }
        return RequestUtils(headers=headers, timeout=self.timeout)

    def _url(self, path: str) -> str:
        """拼装完整 URL（host 已带尾部斜杠）。"""
        return f"{self.host}{str(path).lstrip('/')}"

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET 并解析 JSON；任何失败都抛 :class:`ClientError`。"""
        if not self.configured:
            raise ClientError(f"Jellyfin「{self.name}」未配置 host/apikey")
        response = None
        try:
            response = self._request_utils().get_res(self._url(path), params=params)
            if response is None:
                raise ClientError(f"Jellyfin「{self.name}」未获取到返回数据，请检查 host/API Key")
            status = safe_int(getattr(response, "status_code", 0))
            if status != 200:
                raise ClientError(f"Jellyfin「{self.name}」请求失败：HTTP {status}")
            try:
                return response.json()
            except Exception as err:  # noqa: BLE001
                raise ClientError(f"Jellyfin「{self.name}」返回数据解析失败：{err}") from err
        except ClientError:
            raise
        except Exception as err:  # noqa: BLE001 - 网络/超时等
            raise ClientError(f"Jellyfin「{self.name}」连接失败：{err}") from err
        finally:
            _close_response(response)

    def test(self) -> Tuple[bool, str]:
        """测试连接：访问 ``System/Info``。"""
        if not self.configured:
            return False, f"Jellyfin「{self.name}」未配置 host/apikey"
        try:
            data = self._get_json("System/Info", params={"api_key": self.apikey})
            server_name = _text((data or {}).get("ServerName")) if isinstance(data, dict) else ""
            suffix = f"（{server_name}）" if server_name else ""
            return True, f"Jellyfin「{self.name}」连接成功{suffix}"
        except ClientError as err:
            return False, str(err)
        except Exception as err:  # noqa: BLE001
            return False, f"Jellyfin「{self.name}」连接失败：{err}"

    # ------------------------------------------------------------------ 列表
    def list_media_items(self) -> Tuple[List[MediaItem], List[str]]:
        """分页拉取全部 Movie/Episode 条目并补齐本地 inode 信息。

        :return: ``(媒体条目列表, 告警列表)``；分页/连接失败抛 :class:`ClientError`，
            单条路径 stat 失败只记录告警不中断。
        """
        if not self.configured:
            raise ClientError(f"Jellyfin「{self.name}」未配置 host/apikey")
        items: List[MediaItem] = []
        warnings: List[str] = []
        path_warnings = 0
        start_index = 0
        for page in range(JELLYFIN_MAX_PAGES):
            payload = self._get_json(
                "Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,SeriesId,ParentId,IndexNumber,ParentIndexNumber",
                    "EnableImages": "false",
                    "Limit": self.page_size,
                    "StartIndex": start_index,
                    "api_key": self.apikey,
                },
            )
            if not isinstance(payload, dict):
                raise ClientError(f"Jellyfin「{self.name}」媒体列表返回格式不正确")
            raw_items = payload.get("Items") or []
            total = safe_int(payload.get("TotalRecordCount"), 0)
            for raw in raw_items:
                item, warn = self._convert_item(raw)
                if warn:
                    if path_warnings < MAX_PATH_WARNINGS:
                        warnings.append(warn)
                        path_warnings += 1
                if item is not None:
                    items.append(item)
            if not raw_items:
                break
            start_index += len(raw_items)
            if total and start_index >= total:
                break
            if len(raw_items) < self.page_size:
                # 服务器提前返回了短页：若 TotalRecordCount 表明还有数据，必须视为抓取失败，
                # 绝不能把“分页截断”当成“媒体全没了”而触发批量删除候选。
                if total and start_index < total:
                    raise ClientError(
                        f"Jellyfin「{self.name}」媒体列表分页不完整："
                        f"应为 {total} 条，实际只返回 {start_index} 条"
                    )
                break
        else:  # pragma: no cover - 防御性分页上限
            warnings.append(
                f"Jellyfin「{self.name}」媒体条目过多（已拉取 {len(items)} 条），后续条目未加载"
            )
        return items, warnings

    def _convert_item(self, raw: Any) -> Tuple[Optional[MediaItem], str]:
        """把 Jellyfin 原始条目转换为 :class:`MediaItem`；返回 ``(item, 告警)``。"""
        if not isinstance(raw, dict):
            return None, ""
        item_id = _text(raw.get("Id"))
        item_type = _text(raw.get("Type"))
        if not item_id or item_type not in {"Movie", "Episode"}:
            # 只处理电影/剧集，其它类型（Series/Folder/BoxSet 等）直接忽略
            return None, ""
        source_path = _text(raw.get("Path"))
        mapped_path = map_path(source_path, self.path_mappings) if source_path else ""
        inode = dev = size = 0
        mtime = 0.0
        warning = ""
        extra_inode_keys: List[Any] = []
        if mapped_path:
            try:
                stat_result = os.stat(mapped_path)
            except OSError:
                warning = f"媒体路径不可访问：{mapped_path}"
            else:
                inode = safe_int(getattr(stat_result, "st_ino", 0))
                dev = safe_int(getattr(stat_result, "st_dev", 0))
                size = safe_int(getattr(stat_result, "st_size", 0))
                mtime = safe_float(getattr(stat_result, "st_mtime", 0.0))
                if os.path.isdir(mapped_path):
                    # Jellyfin 可能把蓝光 BDMV 目录整体识别为一个 Movie 条目；
                    # 此时目录 inode 无法与种子视频文件匹配，需展开目录内视频 inode。
                    try:
                        try:
                            from .linker import FileSystemWalker
                        except ImportError:  # 单元测试/独立脚本
                            from linker import FileSystemWalker  # type: ignore
                        walker = FileSystemWalker(max_files_per_torrent=20000, max_depth=8)
                        video_keys = walker.collect_video_inodes(mapped_path)
                    except Exception as err:  # noqa: BLE001 - 目录展开失败只降级
                        logger.warning(f"展开目录型媒体失败（{mapped_path}）：{err}")
                        video_keys = []
                    if video_keys:
                        # FileSystemWalker 的 InodeKey 是 (dev, inode)，不要写反
                        dev, inode = int(video_keys[0][0]), int(video_keys[0][1])
                        extra_inode_keys = [
                            (int(key[0]), int(key[1])) for key in video_keys[1:]
                        ]
        item = MediaItem(
            server=self.name,
            item_id=item_id,
            path=mapped_path,
            inode=inode,
            dev=dev,
            size=size,
            mtime=mtime,
            name=_text(raw.get("Name")),
            item_type=item_type,
            series_name=_text(raw.get("SeriesName")),
            season=safe_int(raw.get("ParentIndexNumber")),
            episode=safe_int(raw.get("IndexNumber")),
            parent_id=_text(raw.get("ParentId")),
            series_id=_text(raw.get("SeriesId")),
            extra_inode_keys=extra_inode_keys,
        )
        return item, warning

    # ------------------------------------------------------------------ 删除
    def delete_item(self, item_id: str) -> Tuple[bool, str, int]:
        """删除 Jellyfin 媒体条目。

        200/204 视为成功；404 视为“已不存在”同样算成功；其它状态码返回失败原因；
        网络异常返回 ``(False, str(err), 0)``。
        """
        target = _text(item_id)
        if not target:
            return False, "无效的媒体条目 ID", 0
        if not self.configured:
            return False, f"Jellyfin「{self.name}」未配置 host/apikey", 0
        response = None
        try:
            response = self._request_utils().delete_res(self._url(f"Items/{target}"))
            if response is None:
                return False, "删除请求未获得响应", 0
            status = safe_int(getattr(response, "status_code", 0))
            if status in (200, 204):
                return True, "", status
            if status == 404:
                return True, "媒体条目已不存在", 404
            return False, f"删除失败：HTTP {status}", status
        except Exception as err:  # noqa: BLE001 - 网络异常不能冒泡
            return False, str(err), 0
        finally:
            _close_response(response)


# ---------------------------------------------------------------------------
# qBittorrent 客户端
# ---------------------------------------------------------------------------
class QbittorrentApi:
    """qBittorrent 下载器客户端封装（依赖 MoviePilot 的 Qbittorrent 实例）。"""

    def __init__(self, name: str, client: Any, normalize_path: Callable[[Any, str], str]):
        self.name = _text(name)
        self.client = client
        self.normalize_path = normalize_path

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _normalize(self, raw_path: Any) -> str:
        """把下载器视角的路径映射为 MoviePilot 视角路径；失败退回原值。"""
        text = _text(raw_path)
        if not text:
            return ""
        func = self.normalize_path
        if callable(func):
            try:
                result = func(raw_path, self.name)
                mapped = _text(result)
                if mapped:
                    return mapped
            except Exception as err:  # noqa: BLE001
                logger.debug(f"qBittorrent「{self.name}」路径映射失败：{text}，{err}")
        return text

    def test(self) -> Tuple[bool, str]:
        """测试连接：尝试拉取一次种子列表。"""
        if self.client is None:
            return False, f"qBittorrent「{self.name}」未初始化"
        try:
            inactive = getattr(self.client, "is_inactive", None)
            if callable(inactive):
                try:
                    if inactive():
                        reconnect = getattr(self.client, "reconnect", None)
                        if callable(reconnect):
                            reconnect()
                except Exception as err:  # noqa: BLE001 - 重连失败交给 get_torrents 报错
                    logger.debug(f"qBittorrent「{self.name}」重连失败：{err}")
            raw_torrents, error = self.client.get_torrents()
        except Exception as err:  # noqa: BLE001
            return False, f"qBittorrent「{self.name}」连接失败：{err}"
        if error:
            return False, f"qBittorrent「{self.name}」获取种子列表失败"
        return True, f"qBittorrent「{self.name}」连接成功（{len(raw_torrents or [])} 个任务）"

    # ------------------------------------------------------------------ 列表
    def list_torrents(self) -> Tuple[List[TorrentItem], List[str]]:
        """拉取全部种子并转换成 :class:`TorrentItem`。

        :return: ``(种子列表, 告警列表)``；查询出错抛 :class:`ClientError`。
        """
        if self.client is None:
            raise ClientError(f"qBittorrent「{self.name}」未初始化")
        try:
            raw_torrents, error = self.client.get_torrents()
        except Exception as err:  # noqa: BLE001
            raise ClientError(f"qBittorrent「{self.name}」获取种子列表异常：{err}") from err
        if error:
            raise ClientError(f"qBittorrent「{self.name}」获取种子列表失败")
        torrents: List[TorrentItem] = []
        warnings: List[str] = []
        for raw in raw_torrents or []:
            try:
                item = self._convert_torrent(raw)
            except Exception as err:  # noqa: BLE001 - 单个种子异常不应中断整体
                warnings.append(f"解析种子失败：{err}")
                continue
            if item is not None:
                torrents.append(item)
        return torrents, warnings

    def _convert_torrent(self, raw: Any) -> Optional[TorrentItem]:
        """把 qB 原始种子转换为 :class:`TorrentItem`。"""
        torrent_hash = _text(_value_of(raw, "hash")).lower()
        if not torrent_hash:
            return None
        save_path = self._normalize(_value_of(raw, "save_path"))
        raw_content_path = _value_of(raw, "content_path")
        content_path = self._normalize(raw_content_path)
        name = _text(_value_of(raw, "name"))
        if not content_path:
            # content_path 为空时退回 save_path/name（单文件种子常见）
            if save_path and name:
                content_path = os.path.normpath(os.path.join(save_path, name))
            else:
                content_path = save_path
        path_exists = bool(content_path) and os.path.exists(content_path)
        # 下载器数据目录在 MoviePilot 容器内不可见（如副 QB 的 /tank/seedings）：
        # 标记为 unsupported，linker 会跳过索引，自动操作不会误删
        unsupported_path = bool(save_path) and not os.path.exists(save_path)
        size = safe_int(_value_of(raw, "total_size")) or safe_int(_value_of(raw, "size"))
        return TorrentItem(
            downloader=self.name,
            hash=torrent_hash,
            name=name,
            category=_text(_value_of(raw, "category")),
            tags=_split_tags(_value_of(raw, "tags")),
            state=_text(_value_of(raw, "state")),
            progress=safe_float(_value_of(raw, "progress")),
            size=size,
            save_path=save_path,
            content_path=content_path,
            added_on=safe_int(_value_of(raw, "added_on")),
            completion_on=safe_int(_value_of(raw, "completion_on")),
            ratio=safe_float(_value_of(raw, "ratio")),
            seeding_time=safe_int(_value_of(raw, "seeding_time")),
            unsupported_path=unsupported_path,
            path_exists=path_exists,
        )

    # ------------------------------------------------------------------ 删除
    def list_files(self, torrent_hash: str) -> Optional[List[str]]:
        """读取某个种子的文件相对路径清单；失败返回 None（由调用方降级处理）。"""
        if self.client is None or not torrent_hash:
            return None
        get_files = getattr(self.client, "get_files", None)
        if not callable(get_files):
            return None
        try:
            files = get_files(str(torrent_hash))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"qBittorrent「{self.name}」读取种子 {torrent_hash} 文件清单失败：{err}")
            return None
        names: List[str] = []
        for item in files or []:
            name = getattr(item, "name", None)
            if name is None and isinstance(item, dict):
                name = item.get("name")
            if name:
                names.append(str(name))
        return names

    def delete_torrents(self, hashes: List[str], delete_files: bool) -> Tuple[bool, str]:
        """删除种子；``delete_files`` 决定是否同时删除下载文件。"""
        if self.client is None:
            return False, f"qBittorrent「{self.name}」未初始化"
        target_hashes = [str(item).strip().lower() for item in (hashes or []) if str(item).strip()]
        if not target_hashes:
            return False, "没有需要删除的种子"
        func = getattr(self.client, "delete_torrents", None)
        if not callable(func):
            return False, f"qBittorrent「{self.name}」客户端不支持删除种子"
        try:
            try:
                result = func(delete_file=bool(delete_files), ids=target_hashes)
            except TypeError:
                # 兼容以位置参数实现的老版本客户端
                result = func(bool(delete_files), target_hashes)
        except Exception as err:  # noqa: BLE001
            return False, f"删除种子失败：{err}"
        if result:
            return True, ""
        return False, "删除种子失败"

    def get_existing_hashes(self, hashes: List[str]) -> set:
        """查询给定 hash 中仍存在于下载器的集合；查询出错抛 :class:`ClientError`。"""
        target_hashes = [str(item).strip().lower() for item in (hashes or []) if str(item).strip()]
        if not target_hashes:
            return set()
        if self.client is None:
            raise ClientError(f"qBittorrent「{self.name}」未初始化")
        try:
            raw_torrents, error = self.client.get_torrents(ids=target_hashes)
        except Exception as err:  # noqa: BLE001
            raise ClientError(f"qBittorrent「{self.name}」查询种子异常：{err}") from err
        if error:
            raise ClientError(f"qBittorrent「{self.name}」查询种子失败")
        existing = set()
        for raw in raw_torrents or []:
            torrent_hash = _text(_value_of(raw, "hash")).lower()
            if torrent_hash:
                existing.add(torrent_hash)
        return existing


# ---------------------------------------------------------------------------
# 客户端集合构建
# ---------------------------------------------------------------------------
def _select_downloader_names(config: PluginConfig, available: Dict[str, Any]) -> List[str]:
    """确定要使用的下载器名称：配置优先；未配置时使用 MoviePilot 默认下载器。"""
    selected = [_text(name) for name in (config.downloaders or []) if _text(name)]
    if selected:
        return selected
    supported = [
        name
        for name, conf in available.items()
        if _text(getattr(conf, "type", "")).lower() in SUPPORTED_DOWNLOADER_TYPES
    ]
    defaults = [name for name in supported if bool(getattr(available.get(name), "default", False))]
    if defaults:
        return defaults[:1]
    return supported[:1]


def _select_media_server_names(config: PluginConfig, available: Dict[str, Any]) -> List[str]:
    """确定要使用的媒体服务器名称：配置优先；未配置时使用全部已启用 Jellyfin。"""
    selected = [_text(name) for name in (config.media_servers or []) if _text(name)]
    if selected:
        return selected
    return [
        name
        for name, conf in available.items()
        if _text(getattr(conf, "type", "")).lower() in SUPPORTED_MEDIA_SERVER_TYPES
    ]


def resolve_service_selection(config: PluginConfig) -> ServiceSelection:
    """解析插件配置实际选择的服务清单（供运行时做覆盖完整性校验）。

    与 :func:`build_client_bundle` 使用相同的选择规则；读取配置失败时返回空清单，
    由运行时按“未配置/覆盖不完整”处理（保守禁止自动删除）。
    """
    downloader_available: Dict[str, Any] = {}
    mediaserver_available: Dict[str, Any] = {}
    try:
        helper = _downloader_helper()
        try:
            downloader_available = _configs_as_dict(helper.get_configs(include_disabled=True))
        except TypeError:  # 兼容旧版 helper 无 include_disabled 参数
            downloader_available = _configs_as_dict(helper.get_configs())
    except Exception as err:  # noqa: BLE001
        logger.error(f"读取 MoviePilot 下载器配置失败：{err}")
    try:
        helper = _mediaserver_helper()
        try:
            mediaserver_available = _configs_as_dict(helper.get_configs(include_disabled=True))
        except TypeError:  # 兼容旧版 helper 无 include_disabled 参数
            mediaserver_available = _configs_as_dict(helper.get_configs())
    except Exception as err:  # noqa: BLE001
        logger.error(f"读取 MoviePilot 媒体服务器配置失败：{err}")

    def _names(available: Dict[str, Any], supported: frozenset, enabled_only: bool) -> List[str]:
        return [
            name
            for name, conf in available.items()
            if _text(getattr(conf, "type", "")).lower() in supported
            and (not enabled_only or getattr(conf, "enabled", True) is not False)
        ]

    # 选择/默认逻辑只使用已启用服务；configured_* 记录全部受支持配置（含禁用）
    enabled_available_downloaders = {
        name: conf
        for name, conf in downloader_available.items()
        if getattr(conf, "enabled", True) is not False
    }
    enabled_available_media_servers = {
        name: conf
        for name, conf in mediaserver_available.items()
        if getattr(conf, "enabled", True) is not False
    }
    return ServiceSelection(
        downloaders=_select_downloader_names(config, enabled_available_downloaders),
        media_servers=_select_media_server_names(config, enabled_available_media_servers),
        enabled_downloaders=_names(
            downloader_available, SUPPORTED_DOWNLOADER_TYPES, enabled_only=True
        ),
        enabled_media_servers=_names(
            mediaserver_available, SUPPORTED_MEDIA_SERVER_TYPES, enabled_only=True
        ),
        configured_downloaders=_names(
            downloader_available, SUPPORTED_DOWNLOADER_TYPES, enabled_only=False
        ),
        configured_media_servers=_names(
            mediaserver_available, SUPPORTED_MEDIA_SERVER_TYPES, enabled_only=False
        ),
    )


def build_client_bundle(config: PluginConfig) -> ClientBundle:
    """根据插件配置构建 Jellyfin/qBittorrent 客户端集合。

    - ``config.downloaders`` 为空 → 使用 MoviePilot 默认下载器；
    - ``config.media_servers`` 为空 → 使用全部已启用的 Jellyfin；
    - 服务不存在、类型不支持、配置缺失等情况写入 ``warnings``，不抛异常；
    - 一个可用客户端都没有时抛 :class:`ClientError`。
    """
    warnings: List[str] = []
    jellyfin_apis: List[JellyfinApi] = []
    qb_apis: List[QbittorrentApi] = []
    missing_jellyfin: List[str] = []
    missing_qbittorrent: List[str] = []
    selected_jellyfin: List[str] = []
    selected_qbittorrent: List[str] = []
    enabled_qbittorrent: List[str] = []

    # ---------------------------------------------------------- 下载器
    downloader_available: Dict[str, Any] = {}
    try:
        helper = _downloader_helper()
        downloader_available = _configs_as_dict(helper.get_configs())
    except Exception as err:  # noqa: BLE001
        logger.error(f"读取 MoviePilot 下载器配置失败：{err}")
        warnings.append("读取 MoviePilot 下载器配置失败，请检查插件配置")
        helper = None

    enabled_qbittorrent = [
        name
        for name, conf in downloader_available.items()
        if _text(getattr(conf, "type", "")).lower() in SUPPORTED_DOWNLOADER_TYPES
        and getattr(conf, "enabled", True) is not False
    ]
    if helper is not None:
        selected_qbittorrent = _select_downloader_names(config, downloader_available)
        for name in selected_qbittorrent:
            try:
                service = helper.get_service(name=name)
            except Exception as err:  # noqa: BLE001
                warnings.append(f"读取下载器「{name}」失败：{err}")
                missing_qbittorrent.append(name)
                continue
            if service is None:
                warnings.append(f"下载器「{name}」不存在或未启用")
                missing_qbittorrent.append(name)
                continue
            service_type = _text(getattr(service, "type", ""))
            if service_type.lower() not in SUPPORTED_DOWNLOADER_TYPES:
                warnings.append(f"下载器「{name}」类型 {service_type or '未知'} 暂不支持（仅支持 qBittorrent）")
                missing_qbittorrent.append(name)
                continue
            instance = getattr(service, "instance", None)
            if instance is None:
                warnings.append(f"下载器「{name}」实例未就绪")
                missing_qbittorrent.append(name)
                continue
            normalizer = _normalizer_from_module(getattr(service, "module", None))
            qb_apis.append(QbittorrentApi(name=name, client=instance, normalize_path=normalizer))

    # ---------------------------------------------------------- 媒体服务器
    mediaserver_available: Dict[str, Any] = {}
    try:
        helper = _mediaserver_helper()
        mediaserver_available = _configs_as_dict(helper.get_configs())
    except Exception as err:  # noqa: BLE001
        logger.error(f"读取 MoviePilot 媒体服务器配置失败：{err}")
        warnings.append("读取 MoviePilot 媒体服务器配置失败，请检查插件配置")
        helper = None

    if helper is not None:
        selected_jellyfin = _select_media_server_names(config, mediaserver_available)
        for name in selected_jellyfin:
            try:
                service = helper.get_service(name=name, type_filter="jellyfin")
            except Exception as err:  # noqa: BLE001
                warnings.append(f"读取媒体服务器「{name}」失败：{err}")
                missing_jellyfin.append(name)
                continue
            if service is None:
                warnings.append(f"媒体服务器「{name}」不存在、未启用或不是 Jellyfin")
                missing_jellyfin.append(name)
                continue
            conf = getattr(service, "config", None)
            conf_data = getattr(conf, "config", None)
            if not isinstance(conf_data, dict):
                conf_data = {}
            host = _text(conf_data.get("host"))
            apikey = _text(conf_data.get("apikey"))
            if not host or not apikey:
                warnings.append(f"媒体服务器「{name}」缺少 host/apikey 配置")
                missing_jellyfin.append(name)
                continue
            jellyfin_apis.append(
                JellyfinApi(
                    name=name,
                    host=host,
                    apikey=apikey,
                    path_mappings=config.media_path_mappings,
                    timeout=30,
                )
            )

    if not jellyfin_apis:
        warnings.append("未选择任何可用的 Jellyfin 媒体服务器")
    if not qb_apis:
        warnings.append("未选择任何可用的 qBittorrent 下载器")
    if not jellyfin_apis and not qb_apis:
        detail = "；".join(warnings) if warnings else "请检查 MoviePilot 服务配置"
        raise ClientError(f"未找到可用的 Jellyfin 媒体服务器或 qBittorrent 下载器：{detail}")
    return ClientBundle(
        jellyfin=jellyfin_apis,
        qbittorrent=qb_apis,
        warnings=warnings,
        missing_jellyfin=missing_jellyfin,
        missing_qbittorrent=missing_qbittorrent,
        selected_jellyfin=selected_jellyfin,
        selected_qbittorrent=selected_qbittorrent,
        enabled_qbittorrent=enabled_qbittorrent,
    )
