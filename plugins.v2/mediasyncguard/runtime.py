"""MediaSyncGuard 运行时：轮询、扫描、待执行动作执行与对外状态。

本模块是插件与 MoviePilot 之间的“胶水层”，负责：

- 通过 :mod:`clients` 抓取 Jellyfin/qBittorrent 数据，并做**失败降级**：任何来源
  抓取失败时用上一次快照补齐，绝不让“抓取失败”被误判成“批量删除”；
- 通过 :mod:`linker` 构建硬链接索引、通过 :mod:`guard` 生成保守级联处理方案；
- 维护待执行动作（宽限期、dry_run、执行前复核）并安全执行；
- 维护扫描后台线程与一次性扫描结果，供 Vue 管理页使用；
- 对外输出契约 §5 规定的 JSON 结构（全部为可 JSON 序列化的 dict/list）。

安全红线（契约 §4.3）：

- 抓取失败来源用旧快照补齐；本轮存在抓取错误时**不执行**任何真实删除；
- 执行任何动作前必须调用 ``guard.recheck_*`` 复核，失败标记 ``skipped``；
- ``dry_run`` 动作永不执行；对应自动开关关闭时真实动作也不执行（保留可取消）；
- 单轮通知去重：同一 ``action_id`` 只通知一次，批量提示每轮最多一条；
- 状态保存失败只记日志，绝不影响 MoviePilot 主流程。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger

try:  # MoviePilot 中作为包模块导入：mediasyncguard.runtime
    from .clients import (
        ClientBundle,
        ClientError,
        JellyfinApi,
        QbittorrentApi,
        build_client_bundle,
        resolve_service_selection,
    )
    from .cleanup import apply_media_cleanup, is_safe_delete_target, plan_media_cleanup
    from .config import PluginConfig
    from .models import (
        LinkIndex,
        MediaItem,
        PendingAction,
        Snapshot,
        TorrentItem,
        media_key,
        safe_float,
        safe_int,
        torrent_key,
    )
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from clients import (  # type: ignore[no-redef]
        ClientBundle,
        ClientError,
        JellyfinApi,
        QbittorrentApi,
        build_client_bundle,
        resolve_service_selection,
    )  # type: ignore[no-redef]
    from cleanup import apply_media_cleanup, is_safe_delete_target, plan_media_cleanup  # type: ignore[no-redef]
    from config import PluginConfig  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        LinkIndex,
        MediaItem,
        PendingAction,
        Snapshot,
        TorrentItem,
        media_key,
        safe_float,
        safe_int,
        torrent_key,
    )

# 整理历史兜底缓存时间（秒）
HISTORY_CACHE_SECONDS = 300.0
# 待执行动作上限（防止无限增长；正常宽限期内远小于该值）
MAX_PENDING_ACTIONS = 200
# status 中候选列表上限
MAX_CANDIDATES = 200
# 通知去重集合上限
MAX_NOTIFIED_IDS = 2000
# 单轮错误通知摘要长度
MAX_ERROR_TEXT = 500
# 下载器“路径不可见/内容缺失”任务占比超过该值时，视为无法校验文件共享
MAX_UNVERIFIABLE_DOWNLOADER_RATIO = 0.05
# 手动删除：单次最多处理条目数（防止一次误选清空整个扫描结果）
MAX_MANUAL_APPLY_ITEMS = 100
# 手动删除：扫描结果最大有效期（秒），过期必须重新扫描
MAX_SCAN_AGE_SECONDS = 600
# 手动删除前若上一轮轮询早于该时间，先同步刷新一次实时状态
MANUAL_APPLY_REFRESH_SECONDS = 30.0

# 信息型（不可动作）候选的 blocked_reason：不进入 status.candidates 展示
INFORMATIONAL_MEDIA_BLOCKED = frozenset({"no_torrent_link", "media_already_gone"})


def _text(value: Any) -> str:
    """安全转换为去空白字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _core_modules() -> Tuple[Any, Any, Any]:
    """延迟导入纯逻辑模块（state/linker/guard）。

    延迟导入的目的一是避免 ``__init__.py`` 与 runtime 的循环依赖，二是便于单元测试
    通过 ``sys.modules`` 注入桩模块。
    """
    try:
        from . import guard as guard_module
        from . import linker as linker_module
        from . import state as state_module
    except ImportError:  # 单元测试/独立脚本（插件目录已加入 sys.path）
        import guard as guard_module  # type: ignore[no-redef]
        import linker as linker_module  # type: ignore[no-redef]
        import state as state_module  # type: ignore[no-redef]
    return state_module, linker_module, guard_module


def _now() -> float:
    return time.time()


class SyncRuntime:
    """MediaSyncGuard 的运行时状态机（插件单例持有）。"""

    def __init__(self, plugin: Any, config: Optional[PluginConfig] = None):
        # plugin 只做 duck-typing 调用：get_data_path() / _notify(title, text) / post_message(...)
        self._plugin = plugin
        if isinstance(config, PluginConfig):
            self._config = config
        else:
            self._config = PluginConfig.from_dict(config or {})

        # 线程安全：_lock 保护状态；_poll_lock 保证同一时间只有一轮监控
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stopping = False

        # 持久化
        self._data_dir: Optional[Path] = None
        self._store: Any = None
        self._state_loaded = False
        self._saved_fingerprint: Any = None
        self._last_save_at = 0.0

        # 监控状态
        self._snapshot = Snapshot()
        self._links = LinkIndex()
        self._pending: List[PendingAction] = []
        self._history: List[Dict[str, Any]] = []
        self._candidates: Dict[str, List[Dict[str, Any]]] = {"torrents": [], "media": []}
        self._last_poll: Dict[str, Any] = self._default_last_poll(0.0)
        self._last_scan_summary: Dict[str, Any] = {}
        self._last_error = ""
        self._warnings: List[str] = []
        self._clients: Optional[ClientBundle] = None
        self._configured = False
        self._has_baseline = False
        self._notified_action_ids: set = set()
        self._last_notified_error = ""
        # 上一轮成功构建索引时的快照内容签名：内容未变化时复用索引，避免每轮遍历磁盘
        self._links_signature: Optional[Tuple[Any, ...]] = None

        # 服务覆盖/可校验性：任何“选中的服务未就绪”或“下载器路径不可见”都会
        # 禁止自动删除/自动删文件，避免执行前复核因缺少数据源而变成“空判断”。
        self._expected_media_servers: set = set()
        self._expected_downloaders: set = set()
        self._enabled_downloaders: set = set()
        self._configured_downloaders: set = set()
        self._configured_media_servers: set = set()
        self._enabled_media_servers: set = set()
        self._last_coverage: Dict[str, Any] = {}
        self._media_to_torrent_verifiable = False
        self._coverage_complete = False
        self._downloaders_verifiable = False
        self._file_deletion_safe = False
        self._coverage_warnings: List[str] = []
        self._exec_config: PluginConfig = self._config

        # 整理历史兜底缓存
        self._history_rows_cache: Optional[List[Tuple[str, str, str]]] = None
        self._history_cache_at = 0.0

        # 扫描状态
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_cancel_event = threading.Event()
        self._scan_state: Dict[str, Any] = self._empty_scan_state()
        self._scan_links: Optional[LinkIndex] = None
        self._scan_snapshot: Optional[Snapshot] = None

        # 插件在首次 init_plugin 时只创建实例、不会另外调用 configure（仅重载配置时调用），
        # 因此构造阶段必须完成一次完整初始化；失败只记录 last_error，不向插件抛异常
        try:
            if not isinstance(config, PluginConfig):
                self._config = PluginConfig.from_dict(config or {})
            self.configure(self._config)
        except Exception as err:  # noqa: BLE001 - 构造阶段绝不能让插件加载失败
            self._last_error = f"插件初始化失败：{err}"[:MAX_ERROR_TEXT]
            logger.error(f"媒体同步守护初始化失败：{err}")

    # ==================================================================
    # 生命周期 / 配置
    # ==================================================================
    def configure(self, config: PluginConfig) -> None:
        """保存配置、重建客户端、加载持久化状态；任何异常只记录不抛出。"""
        try:
            new_config = (
                config if isinstance(config, PluginConfig) else PluginConfig.from_dict(config or {})
            )
            old_config = self._config or PluginConfig()
            with self._lock:
                self._config = new_config
                self._exec_config = new_config
                self._stopping = False
                if not self._state_loaded:
                    self._load_state()
                elif old_config.dry_run and not new_config.dry_run:
                    # 演练模式关闭：旧演练动作永远不会执行，清掉以便生成真实动作
                    self._drop_dry_run_actions("演练模式已关闭，旧的演练动作已失效")
                self._rebuild_clients()
                # 配置变化后旧的候选已无意义；扫描结果依赖服务选择，也一并清空
                self._candidates = {"torrents": [], "media": []}
                self._scan_links = None
                self._scan_snapshot = None
                self._links_signature = None
                with self._scan_lock:
                    if self._scan_thread is None or not self._scan_thread.is_alive():
                        self._scan_state = self._empty_scan_state()
            self._persist(force=True)
        except Exception as err:  # noqa: BLE001 - 契约要求 configure 绝不向插件抛异常
            self._last_error = f"插件配置初始化失败：{err}"
            logger.error(f"媒体同步守护初始化失败：{err}")

    def stop(self) -> None:
        """停止扫描线程并保存状态。"""
        self._stopping = True
        try:
            self.cancel_scan()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"媒体同步守护停止扫描失败：{err}")
        thread = self._scan_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._persist(force=True)

    # ==================================================================
    # 持久化
    # ==================================================================
    def _resolve_data_dir(self) -> Path:
        """插件数据目录（失败时回退到系统临时目录，保证功能可用）。"""
        try:
            getter = getattr(self._plugin, "get_data_path", None)
            if callable(getter):
                path = getter()
                if path:
                    return Path(path)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"媒体同步守护获取插件数据目录失败：{err}")
        import tempfile

        return Path(tempfile.gettempdir()) / "mediasyncguard"

    def _load_state(self) -> None:
        """加载持久化状态（仅首次 configure 时执行）。"""
        state_module = _core_modules()[0]
        data_dir = self._data_dir or self._resolve_data_dir()
        self._data_dir = data_dir
        store = state_module.StateStore(data_dir)
        self._store = store
        persisted = store.load()
        with self._lock:
            snapshot = getattr(persisted, "snapshot", None)
            self._snapshot = snapshot if isinstance(snapshot, Snapshot) else Snapshot()
            # 不持久化硬链接索引：它随文件系统状态变化、体积远大于快照，启动后第一轮会重建。
            # 缺少上一轮索引时自动删除只会降级为“不动作”（扫描页仍可人工确认），
            # 既避免每轮/启动时数 MB 的索引序列化 IO，也不会误删。
            self._links = LinkIndex()
            self._pending = []
            self._history = []
            for action in getattr(persisted, "pending", None) or []:
                if not isinstance(action, PendingAction):
                    continue
                if action.is_pending:
                    self._pending.append(action)
                else:
                    self._append_history(
                        {
                            "action_id": action.action_id,
                            "kind": action.kind,
                            "status": action.status,
                            "finished_at": _now(),
                            "summary": "启动时恢复的历史动作",
                        }
                    )
            self._trim_pending()
            if not getattr(self._config, "dry_run", True):
                # 配置已关闭演练模式：上次进程遗留的演练动作永远不会执行，直接作废
                self._drop_dry_run_actions("演练模式已关闭，启动时作废旧演练动作")
            last_poll_at = safe_float(getattr(persisted, "last_poll_at", 0.0))
            self._last_poll = self._default_last_poll(last_poll_at)
            summary = getattr(persisted, "last_scan_summary", None)
            self._last_scan_summary = dict(summary) if isinstance(summary, dict) else {}
            self._last_error = _text(getattr(persisted, "last_error", ""))
            self._has_baseline = bool(self._snapshot.media or self._snapshot.torrents)
            self._saved_fingerprint = self._state_fingerprint()
            self._links_signature = None
            self._state_loaded = True

    def _default_last_poll(self, at: float) -> Dict[str, Any]:
        """契约 §5.1 的 last_poll 结构默认值。"""
        return {
            "at": safe_float(at),
            "duration": 0.0,
            "ok": False,
            "partial": False,
            "errors": [],
            "warnings": [],
            "media_total": 0,
            "torrent_total": 0,
            "links": 0,
            "lost_media": 0,
            "lost_torrents": 0,
            "actions_created": 0,
            "actions_executed": 0,
        }

    def _state_fingerprint(self) -> Tuple[Any, ...]:
        """快照 + 待办的状态指纹，用于决定是否需要落盘。"""
        try:
            media_hash = hash(frozenset(self._snapshot.media.keys()))
            torrent_hash = hash(frozenset(self._snapshot.torrents.keys()))
            pending = tuple(sorted((item.action_id, item.status) for item in self._pending))
            return (media_hash, torrent_hash, pending)
        except Exception:  # noqa: BLE001 - 指纹只用于节流
            return (id(self._snapshot), id(self._pending))

    def _persist(self, force: bool = False) -> None:
        """保存状态；仅在快照成员/待办变化或强制保存时落盘，避免周期性大量写 IO。

        硬链接索引不落盘（启动后第一轮重建）：它体积大且与文件系统强相关，
        持久化收益低；缺失上一轮索引时自动删除会保守地不动作。
        """
        if self._store is None:
            return
        try:
            fingerprint = self._state_fingerprint()
            now = _now()
            if not force and fingerprint == self._saved_fingerprint:
                return
            state_module = _core_modules()[0]
            with self._lock:
                persisted = state_module.PersistedState(
                    snapshot=self._snapshot,
                    links=LinkIndex(),
                    pending=list(self._pending),
                    last_poll_at=safe_float(self._last_poll.get("at") or 0.0),
                    last_scan_summary=dict(self._last_scan_summary),
                    last_error=self._last_error,
                )
                self._store.save(persisted)
            self._saved_fingerprint = fingerprint
            self._last_save_at = now
        except Exception as err:  # noqa: BLE001 - 持久化失败不能影响主流程
            logger.error(f"媒体同步守护保存状态失败：{err}")

    # ==================================================================
    # 客户端
    # ==================================================================
    def _rebuild_clients(self) -> None:
        """根据当前配置重建客户端集合；失败写入 warnings/last_error，不抛出。"""
        warnings: List[str] = []
        bundle: Optional[ClientBundle] = None
        try:
            bundle = build_client_bundle(self._config)
        except ClientError as err:
            self._clients = None
            self._configured = False
            warnings.append(f"客户端初始化失败：{err}")
            self._last_error = str(err)[:MAX_ERROR_TEXT]
        except Exception as err:  # noqa: BLE001
            self._clients = None
            self._configured = False
            warnings.append(f"客户端初始化失败：{err}")
            self._last_error = f"客户端初始化失败：{err}"[:MAX_ERROR_TEXT]
        else:
            self._clients = bundle
            warnings.extend(bundle.warnings or [])
            self._configured = bool(bundle.jellyfin or bundle.qbittorrent)
            if not self._configured:
                self._last_error = "未检测到可用的 Jellyfin / qBittorrent 服务"
        # 记录配置期望的服务清单：每轮抓取后据此判断“覆盖是否完整”。
        # 选中服务在构建阶段就缺失时，直接标记覆盖不完整并写入告警。
        try:
            selection = resolve_service_selection(self._config)
            self._expected_media_servers = set(selection.media_servers)
            self._expected_downloaders = set(selection.downloaders)
            self._enabled_downloaders = set(selection.enabled_downloaders)
            self._configured_downloaders = set(selection.configured_downloaders)
            self._configured_media_servers = set(selection.configured_media_servers)
            self._enabled_media_servers = set(selection.enabled_media_servers)
        except Exception as err:  # noqa: BLE001
            warnings.append(f"解析服务选择失败：{err}")
            self._expected_media_servers = set()
            self._expected_downloaders = set()
            self._enabled_downloaders = set()
            self._configured_downloaders = set()
            self._configured_media_servers = set()
            self._enabled_media_servers = set()
        # 配置中存在但未纳入监控的下载器/媒体服务器：其数据无法参与共享校验，
        # 保守起见整体降级（下载器影响删文件/删媒体；媒体服务器影响自动删种）。
        unselected_configured = set(self._configured_downloaders) - set(
            self._expected_downloaders
        )
        if unselected_configured:
            warnings.append(
                "存在未纳入监控的下载器（"
                + "、".join(sorted(unselected_configured))
                + "），无法校验其共享文件；已禁用自动删文件/自动删媒体"
            )
        unselected_media_servers = set(self._configured_media_servers) - set(
            self._expected_media_servers
        )
        if unselected_media_servers:
            warnings.append(
                "存在未纳入监控的媒体服务器（含已禁用，"
                + "、".join(sorted(unselected_media_servers))
                + "），无法确认媒体是否已在其它服务器移除；已禁用自动删种"
            )
        if bundle is not None:
            # 服务发现不可用时的兜底：至少以实际构建出的客户端作为期望来源，
            # 避免期望集合为空导致“覆盖校验”形同虚设。
            if not self._expected_downloaders:
                self._expected_downloaders = {api.name for api in bundle.qbittorrent}
            if not self._expected_media_servers:
                self._expected_media_servers = {api.name for api in bundle.jellyfin}
        missing_services: set = set()
        if bundle is not None:
            missing_services = set(getattr(bundle, "missing_jellyfin", None) or []) | set(
                getattr(bundle, "missing_qbittorrent", None) or []
            )
        if not self._expected_media_servers and self._configured:
            warnings.append("未选择任何 Jellyfin 媒体服务器，自动删种已禁用")
        if not self._expected_downloaders and self._configured:
            warnings.append("未选择任何 qBittorrent 下载器，自动删媒体已禁用")
        if missing_services:
            warnings.append(
                "以下选中服务未就绪，已暂停自动删除：" + "、".join(sorted(missing_services))
            )
        self._warnings = self._dedupe(warnings)
        self._coverage_complete = self._configured and not missing_services

    def _dedupe(self, items: List[str]) -> List[str]:
        """字符串列表去重（保序）。"""
        result: List[str] = []
        for item in items or []:
            text = _text(item)
            if text and text not in result:
                result.append(text)
        return result

    def _find_jellyfin(self, name: str) -> Optional[JellyfinApi]:
        if not self._clients:
            return None
        for api in self._clients.jellyfin:
            if api.name == name:
                return api
        return None

    def _find_qbittorrent(self, name: str) -> Optional[QbittorrentApi]:
        if not self._clients:
            return None
        for api in self._clients.qbittorrent:
            if api.name == name:
                return api
        return None

    # ==================================================================
    # 通知
    # ==================================================================
    def _notify(self, title: str, text: str) -> None:
        """发送中文通知：优先插件 ``_notify``，其次 ``post_message``。"""
        if not getattr(self._config, "notify", False):
            return
        try:
            notify = getattr(self._plugin, "_notify", None)
            if callable(notify):
                notify(title, text)
                return
            post = getattr(self._plugin, "post_message", None)
            if callable(post):
                from app.schemas.types import NotificationType

                post(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:  # noqa: BLE001 - 通知失败不影响主流程
            logger.warning(f"媒体同步守护发送通知失败：{err}")

    # ==================================================================
    # 监控轮询
    # ==================================================================
    def scheduled_poll(self) -> None:
        """MoviePilot 定时任务入口：非阻塞锁，忙则跳过本轮。"""
        if self._stopping or not getattr(self._config, "enabled", False):
            return
        if not self._poll_lock.acquire(blocking=False):
            logger.info("媒体同步守护：上一轮监控尚未结束，跳过本轮")
            return
        try:
            self._poll_impl()
        except Exception as err:  # noqa: BLE001 - 定时任务绝不能让异常冒泡
            self._last_error = f"监控轮询失败：{err}"[:MAX_ERROR_TEXT]
            logger.error(f"媒体同步守护监控轮询失败：{err}")
        finally:
            self._poll_lock.release()

    def poll_once(self) -> Dict[str, Any]:
        """同步执行一轮监控（等待上一轮结束），返回 last_poll 摘要。"""
        with self._poll_lock:
            try:
                return self._poll_impl()
            except Exception as err:  # noqa: BLE001
                self._last_error = f"监控轮询失败：{err}"[:MAX_ERROR_TEXT]
                logger.error(f"媒体同步守护监控轮询失败：{err}")
                summary = self._default_last_poll(_now())
                summary.update(ok=False, errors=[str(err)])
                self._last_poll = summary
                return summary

    def trigger_poll(self) -> Dict[str, Any]:
        """手动触发一轮监控；上一轮未结束时返回当前摘要并提示。

        插件未启用/正在停止时只允许抓取状态（不产生也不执行删除动作）。
        """
        if not self._poll_lock.acquire(blocking=False):
            summary = dict(self._last_poll or self._default_last_poll(0.0))
            summary["skipped"] = True
            summary["message"] = "上一轮监控尚未结束，已跳过"
            return summary
        try:
            return self._poll_impl()
        except Exception as err:  # noqa: BLE001
            self._last_error = f"监控轮询失败：{err}"[:MAX_ERROR_TEXT]
            logger.error(f"媒体同步守护手动轮询失败：{err}")
            summary = dict(self._last_poll or self._default_last_poll(0.0))
            summary.update(ok=False, errors=[str(err)])
            return summary
        finally:
            self._poll_lock.release()

    def _content_signature(self, snapshot: Snapshot) -> Optional[Tuple[Any, ...]]:
        """快照内容签名：媒体/种子的关键字段未变化时可以复用上一轮硬链接索引。"""
        try:
            media = tuple(
                sorted(
                    (
                        key,
                        str(item.path or ""),
                        int(item.inode or 0),
                        int(item.dev or 0),
                    )
                    for key, item in snapshot.media.items()
                )
            )
            torrents = tuple(
                sorted(
                    (
                        key,
                        str(item.content_path or ""),
                        bool(item.path_exists),
                        bool(item.unsupported_path),
                    )
                    for key, item in snapshot.torrents.items()
                )
            )
            return media, torrents
        except Exception:  # noqa: BLE001 - 签名仅用于性能优化
            return None

    def _fetch_snapshots(self, now: float) -> Tuple[
        Snapshot, Optional[Snapshot], List[str], List[str], Dict[str, Any]
    ]:
        """抓取本轮媒体/种子快照，并对失败来源用上一份快照做安全补齐。

        :return: ``(current, previous_view, fetch_errors, warnings, coverage)``；
            ``previous_view`` 为“本轮仍跟踪的来源”的上一轮快照（首次运行返回 None）；
            ``coverage`` 记录本轮成功/失败的服务名，供上层判断覆盖是否完整。
        """
        errors: List[str] = []
        warnings: List[str] = []
        current_media: Dict[str, MediaItem] = {}
        current_torrents: Dict[str, TorrentItem] = {}
        media_ok: set = set()
        media_failed: set = set()
        torrent_ok: set = set()
        torrent_failed: set = set()

        bundle = self._clients
        if bundle is None:
            return (
                Snapshot(created_at=now),
                None,
                ["未配置可用的服务客户端"],
                [],
                {
                    "media_ok": set(),
                    "torrent_ok": set(),
                    "media_failed": set(),
                    "torrent_failed": set(),
                },
            )

        for api in bundle.jellyfin:
            try:
                items, api_warnings = api.list_media_items()
            except ClientError as err:
                media_failed.add(api.name)
                errors.append(f"Jellyfin「{api.name}」读取失败：{err}")
                continue
            except Exception as err:  # noqa: BLE001
                media_failed.add(api.name)
                errors.append(f"Jellyfin「{api.name}」读取异常：{err}")
                continue
            media_ok.add(api.name)
            warnings.extend(api_warnings or [])
            for item in items or []:
                current_media[item.key] = item

        for api in bundle.qbittorrent:
            try:
                items, api_warnings = api.list_torrents()
            except ClientError as err:
                torrent_failed.add(api.name)
                errors.append(f"qBittorrent「{api.name}」读取失败：{err}")
                continue
            except Exception as err:  # noqa: BLE001
                torrent_failed.add(api.name)
                errors.append(f"qBittorrent「{api.name}」读取异常：{err}")
                continue
            torrent_ok.add(api.name)
            warnings.extend(api_warnings or [])
            for item in items or []:
                current_torrents[item.key] = item

        # 抓取失败的来源：用上一轮快照补齐，绝不能把“失败”当成“全部消失”
        for key, item in self._snapshot.media.items():
            if item.server in media_failed and key not in current_media:
                current_media[key] = item
        for key, item in self._snapshot.torrents.items():
            if item.downloader in torrent_failed and key not in current_torrents:
                current_torrents[key] = item

        current = Snapshot(media=current_media, torrents=current_torrents, created_at=now)

        # 上一轮快照只保留本轮仍跟踪的来源：取消勾选的服务不会被误判为“媒体消失”
        tracked_media_servers = media_ok | media_failed
        tracked_downloaders = torrent_ok | torrent_failed
        previous_view: Optional[Snapshot] = None
        if self._has_baseline:
            previous_view = Snapshot(
                media={
                    key: item
                    for key, item in self._snapshot.media.items()
                    if item.server in tracked_media_servers
                },
                torrents={
                    key: item
                    for key, item in self._snapshot.torrents.items()
                    if item.downloader in tracked_downloaders
                },
                created_at=safe_float(self._snapshot.created_at),
            )
        coverage = {
            "media_ok": set(media_ok),
            "torrent_ok": set(torrent_ok),
            "media_failed": set(media_failed),
            "torrent_failed": set(torrent_failed),
        }
        return current, previous_view, errors, warnings, coverage

    def _compute_downloader_verifiability(
        self, snapshot: Snapshot
    ) -> Tuple[bool, List[str]]:
        """判断当前选中的下载器是否足以校验“文件是否仍被其它种子引用”。

        下载器数据目录在 MoviePilot 容器内不可见（例如副QB 的 /tank/seedings）时，
        其种子无法参与 inode 关联，会让“无现存种子引用”的复核变成空判断。
        因此只要存在路径不可见/内容缺失比例偏高的下载器，就判定为不可校验，
        由上层禁用自动删媒体与删文件。
        """
        warnings: List[str] = []
        bundle = self._clients
        if bundle is None or not bundle.qbittorrent:
            return False, ["没有可用的下载器客户端，无法校验文件共享"]
        verifiable = True
        for api in bundle.qbittorrent:
            torrents = [
                item for item in snapshot.torrents.values() if item.downloader == api.name
            ]
            total = len(torrents)
            if total == 0:
                warnings.append(
                    f"下载器「{api.name}」本轮未读取到任何任务，无法校验文件共享；"
                    f"已禁用自动删文件/自动删媒体"
                )
                verifiable = False
                continue
            unknown = sum(
                1 for item in torrents if item.unsupported_path or not item.path_exists
            )
            ratio = unknown / total
            if ratio > MAX_UNVERIFIABLE_DOWNLOADER_RATIO:
                warnings.append(
                    f"下载器「{api.name}」有 {unknown}/{total} 个任务路径不可见或内容缺失"
                    f"（{ratio:.1%}），无法校验文件共享；已禁用自动删文件/自动删媒体"
                )
                verifiable = False
        return verifiable, warnings

    def _filter_media_candidates(self, candidates: List[Any]) -> List[Dict[str, Any]]:
        """过滤信息型媒体候选，只保留 UI 可操作的条目。"""
        result: List[Dict[str, Any]] = []
        for candidate in candidates or []:
            try:
                data = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
            except Exception:  # noqa: BLE001
                continue
            if _text(data.get("reason")) == "media_deleted":
                continue
            if _text(data.get("blocked_reason")) in INFORMATIONAL_MEDIA_BLOCKED:
                continue
            result.append(data)
            if len(result) >= MAX_CANDIDATES:
                break
        return result

    def _filter_torrent_candidates(self, candidates: List[Any]) -> List[Dict[str, Any]]:
        """转换 torrent 候选为 status 结构。"""
        result: List[Dict[str, Any]] = []
        for candidate in candidates or []:
            try:
                data = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
            except Exception:  # noqa: BLE001
                continue
            result.append(data)
            if len(result) >= MAX_CANDIDATES:
                break
        return result

    def _merge_actions(self, actions: List[PendingAction]) -> List[PendingAction]:
        """把新生成的待执行动作合并进 pending（按 action_id 去重）。"""
        created: List[PendingAction] = []
        existing = {action.action_id for action in self._pending}
        for action in actions or []:
            if not isinstance(action, PendingAction):
                continue
            if not action.action_id or action.action_id in existing:
                continue
            self._pending.append(action)
            existing.add(action.action_id)
            created.append(action)
        if created:
            self._trim_pending()
        return created

    def _trim_pending(self) -> None:
        """限制 pending 长度，优先保留未过期的真实动作。"""
        if len(self._pending) <= MAX_PENDING_ACTIONS:
            return
        overflow = len(self._pending) - MAX_PENDING_ACTIONS
        logger.warning(f"媒体同步守护待执行动作过多，丢弃最早的 {overflow} 条")
        removed = self._pending[:overflow]
        self._pending = self._pending[overflow:]
        for action in removed:
            self._append_history(
                {
                    "action_id": action.action_id,
                    "kind": action.kind,
                    "status": "cancelled",
                    "finished_at": _now(),
                    "summary": "待执行动作过多，已自动丢弃",
                }
            )

    def _notify_notices(self, notices: List[str]) -> None:
        """批量/演练跳过提示：每轮最多一条通知。"""
        texts = [text for text in (notices or []) if _text(text)]
        if not texts:
            return
        self._notify("媒体同步守护", "；".join(texts[:3]))

    def _notify_dry_run_candidates(
        self, diff: Any, plan: Any, created_actions: List[PendingAction]
    ) -> None:
        """演练模式候选通知（同一批动作只通知一次）。"""
        if not getattr(self._config, "dry_run", False):
            return
        new_ids = [
            action.action_id
            for action in created_actions or []
            if action.dry_run and action.action_id not in self._notified_action_ids
        ]
        if not new_ids:
            return
        lost_media = len(getattr(diff, "lost_media", None) or [])
        eligible = sum(
            1
            for candidate in (getattr(plan, "torrent_candidates", None) or [])
            if getattr(candidate, "auto_eligible", False)
        )
        self._notify(
            "媒体同步守护（演练）",
            f"发现 {lost_media} 个媒体条目已从 Jellyfin 删除，"
            f"{eligible} 个关联种子待处理；未执行删除。",
        )
        self._remember_notified(new_ids)

    def _remember_notified(self, action_ids: List[str]) -> None:
        for action_id in action_ids or []:
            if action_id:
                self._notified_action_ids.add(action_id)
        if len(self._notified_action_ids) > MAX_NOTIFIED_IDS:
            # 简单裁剪：保留最近的一半
            keep = list(self._notified_action_ids)[-MAX_NOTIFIED_IDS // 2:]
            self._notified_action_ids = set(keep)

    def _notify_errors(self, errors: List[str]) -> None:
        """出错通知（相同错误摘要只通知一次，避免每轮刷屏）。"""
        if not errors:
            return
        summary = "；".join(errors[:3])[:MAX_ERROR_TEXT]
        if summary == self._last_notified_error:
            return
        self._last_notified_error = summary
        self._notify("媒体同步守护", f"运行出错：{summary}，请检查插件配置。")

    def _poll_impl(self) -> Dict[str, Any]:
        """执行一轮完整监控，返回 last_poll 摘要。"""
        started_mono = time.monotonic()
        now = _now()
        errors: List[str] = []
        warnings: List[str] = []
        summary = self._default_last_poll(now)
        summary["at"] = now

        if not self._clients or not self._configured:
            message = "；".join(self._warnings) or "未配置可用的 Jellyfin / qBittorrent 服务"
            summary.update(
                duration=round(time.monotonic() - started_mono, 3),
                ok=False,
                partial=True,
                errors=[message],
            )
            with self._lock:
                self._last_poll = summary
                self._last_error = message[:MAX_ERROR_TEXT]
            self._notify_errors([message])
            return summary

        state_module, linker_module, guard_module = _core_modules()
        # 注意：抓取是网络/文件系统 IO，不能持有 self._lock（否则 get_status 会被阻塞）
        current, previous_view, fetch_errors, fetch_warnings, coverage = self._fetch_snapshots(now)
        errors.extend(fetch_errors or [])
        warnings.extend(fetch_warnings or [])

        # 每轮重新解析服务选择：运行中在 MoviePilot 里新增/禁用服务也能及时感知（M-F）。
        try:
            selection = resolve_service_selection(self._config)
            if selection.downloaders:
                self._expected_downloaders = set(selection.downloaders)
            if selection.media_servers:
                self._expected_media_servers = set(selection.media_servers)
            self._enabled_downloaders = set(selection.enabled_downloaders)
            self._configured_downloaders = set(selection.configured_downloaders)
            self._configured_media_servers = set(selection.configured_media_servers)
            self._enabled_media_servers = set(selection.enabled_media_servers)
        except Exception as err:  # noqa: BLE001 - 解析失败沿用上一轮选择
            logger.warning(f"媒体同步守护刷新服务选择失败（沿用上一轮）：{err}")

        # 覆盖校验：选中的服务只要有一个本轮没成功读取，就视为“数据不完整”，
        # 暂停自动生成/执行删除，避免复核因缺少数据源而空判断。
        missing_media_servers = set(self._expected_media_servers) - set(
            coverage.get("media_ok") or set()
        )
        missing_downloaders = set(self._expected_downloaders) - set(
            coverage.get("torrent_ok") or set()
        )
        missing_services = missing_media_servers | missing_downloaders
        if not missing_services:
            # M-A：期望集合为空时也不能把“没有任何媒体/种子数据源”当成完整覆盖。
            if not coverage.get("media_ok"):
                missing_services.add("(无可用 Jellyfin 数据源)")
            if not coverage.get("torrent_ok"):
                missing_services.add("(无可用 qBittorrent 数据源)")
        if missing_services:
            errors.append(
                "以下数据源本轮不可用，已暂停自动删除："
                + "、".join(sorted(missing_services))
            )
        fetch_partial = bool(fetch_errors) or bool(missing_services)

        # 下载器可校验性：路径不可见的下载器（副QB 等）无法参与共享判断，
        # 一旦存在就禁用自动删文件/自动删媒体；配置中存在但未纳管的下载器
        # （包括在 MoviePilot 中被临时禁用的实例）同样按不可校验处理。
        downloaders_verifiable, verify_warnings = self._compute_downloader_verifiability(current)
        unselected_downloaders = sorted(
            set(self._configured_downloaders) - set(self._expected_downloaders)
        )
        if unselected_downloaders:
            downloaders_verifiable = False
            verify_warnings.append(
                "存在未纳入监控的下载器（"
                + "、".join(unselected_downloaders)
                + "），无法校验其共享文件；已禁用自动删文件/自动删媒体"
            )
        # M-E：存在未纳入监控的 Jellyfin（含在 MoviePilot 中被临时禁用但仍在运行的实例）
        # 时，无法证明媒体在其它服务器上不存在，自动删种必须停用。
        unselected_media_servers = sorted(
            set(self._configured_media_servers) - set(self._expected_media_servers)
        )
        media_to_torrent_verifiable = not unselected_media_servers
        if not media_to_torrent_verifiable:
            verify_warnings.append(
                "存在未纳入监控的媒体服务器（含已禁用，"
                + "、".join(unselected_media_servers)
                + "），无法确认媒体是否已在其它服务器移除；已禁用自动删种"
            )
        warnings.extend(verify_warnings)
        file_deletion_safe = bool(downloaders_verifiable and not fetch_partial)
        actions_allowed = bool(getattr(self._config, "enabled", False)) and not self._stopping
        exec_config = self._config
        overrides: Dict[str, Any] = {}
        if fetch_partial:
            overrides.update(
                auto_media_to_torrent=False,
                auto_torrent_to_media=False,
                auto_delete_files=False,
            )
        if not downloaders_verifiable:
            overrides.update(auto_torrent_to_media=False, auto_delete_files=False)
        if not media_to_torrent_verifiable:
            overrides.update(auto_media_to_torrent=False)
        if overrides:
            exec_config = self._config.merged(**overrides)
        with self._lock:
            self._coverage_complete = not fetch_partial
            self._downloaders_verifiable = bool(downloaders_verifiable)
            self._file_deletion_safe = file_deletion_safe
            self._media_to_torrent_verifiable = bool(media_to_torrent_verifiable)
            self._last_coverage = coverage
            self._coverage_warnings = self._dedupe(verify_warnings)
            self._exec_config = exec_config

        history_rows: List[Tuple[str, str, str]] = []
        try:
            history_rows = self._read_history_rows()
        except Exception as err:  # noqa: BLE001 - 历史兜底失败不影响主流程
            logger.warning(f"媒体同步守护读取整理历史失败（忽略兜底）：{err}")

        links_ok = False
        links = self._links
        content_signature = self._content_signature(current)
        reuse_links = bool(
            self._has_baseline
            and content_signature is not None
            and self._links_signature == content_signature
        )
        if reuse_links:
            # 媒体/种子内容与上一轮完全一致：复用上一轮索引，避免每轮全盘遍历
            links_ok = True
        else:
            try:
                builder = linker_module.LinkBuilder()
                build = builder.build(
                    list(current.media.values()),
                    list(current.torrents.values()),
                    history_rows,
                )
                links = build.index
                links_ok = True
            except Exception as err:  # noqa: BLE001
                errors.append(f"构建硬链接索引失败：{err}")
                logger.error(f"媒体同步守护构建硬链接索引失败：{err}")

        plan = None
        created_actions: List[PendingAction] = []
        diff = None
        if links_ok:
            try:
                diff = state_module.diff_snapshots(previous_view, current)
            except Exception as err:  # noqa: BLE001
                errors.append(f"快照对比失败：{err}")
                diff = state_module.diff_snapshots(None, current)
            plan_previous = previous_view if previous_view is not None else Snapshot()
            if not fetch_partial and actions_allowed:
                existing_ids = {
                    action.action_id for action in self._pending if not action.dry_run
                }
                try:
                    plan = guard_module.plan_cycle(
                        exec_config,
                        diff,
                        plan_previous,
                        current,
                        self._links,
                        links,
                        existing_action_ids=existing_ids,
                        now=now,
                    )
                except Exception as err:  # noqa: BLE001
                    errors.append(f"生成处理方案失败：{err}")
                    logger.error(f"媒体同步守护生成处理方案失败：{err}")

        with self._lock:
            if plan is not None:
                self._candidates = {
                    "torrents": self._filter_torrent_candidates(
                        getattr(plan, "torrent_candidates", None)
                    ),
                    "media": self._filter_media_candidates(
                        getattr(plan, "media_candidates", None)
                    ),
                }
                created_actions = self._merge_actions(getattr(plan, "actions", None))
            if links_ok:
                # 只有索引构建/复用成功才更新基线，避免“半成品快照”影响下一次 diff
                self._snapshot = current
                self._links = links
                self._has_baseline = True

        if plan is not None:
            self._notify_notices(getattr(plan, "notices", None) or [])
            self._notify_dry_run_candidates(diff, plan, created_actions)

        executed = 0
        if not fetch_partial and links_ok and actions_allowed:
            try:
                executed = self._execute_due_actions(now)
            except Exception as err:  # noqa: BLE001 - 执行异常不能中断轮询
                errors.append(f"执行待办动作失败：{err}")
                logger.error(f"媒体同步守护执行待办动作失败：{err}")

        summary.update(
            duration=round(time.monotonic() - started_mono, 3),
            ok=not errors,
            partial=fetch_partial,
            errors=errors,
            warnings=self._dedupe(warnings)[:20],
            media_total=current.media_count,
            torrent_total=current.torrent_count,
            links=int(getattr(links, "link_count", 0) or 0),
            lost_media=len(getattr(diff, "lost_media", None) or []) if diff is not None else 0,
            lost_torrents=(
                len(getattr(diff, "lost_torrents", None) or []) if diff is not None else 0
            ),
            actions_created=len(created_actions),
            actions_executed=executed,
        )
        with self._lock:
            self._last_poll = summary
            self._last_error = ("；".join(errors)[:MAX_ERROR_TEXT] if errors else "")
            if errors:
                pass
            else:
                # 本轮无错误：允许下次出现相同错误时重新通知
                self._last_notified_error = ""
            if links_ok and executed == 0:
                # 记录内容签名：下一轮内容不变时可直接复用索引
                self._links_signature = self._content_signature(self._snapshot)
            else:
                self._links_signature = None
        self._notify_errors(errors)
        self._persist()
        return summary

    # ==================================================================
    # 整理历史兜底
    # ==================================================================
    def _read_history_rows(self) -> List[Tuple[str, str, str]]:
        """读取 MoviePilot 成功整理记录（downloader, hash, dest），缓存 5 分钟。"""
        now = time.monotonic()
        if (
            self._history_rows_cache is not None
            and (now - self._history_cache_at) < HISTORY_CACHE_SECONDS
        ):
            return self._history_rows_cache
        rows: List[Tuple[str, str, str]] = []
        try:
            from app.db.models.transferhistory import TransferHistory
            from app.db.transferhistory_oper import TransferHistoryOper

            oper = TransferHistoryOper()
            db = getattr(oper, "_db", None)
            # 只取最近一部分成功记录作为兜底：完整历史可能很大，且兜底关联不要求全量
            records = TransferHistory.list_by_page(db, page=1, count=5000, status=True)
            for record in records or []:
                downloader = _text(getattr(record, "downloader", None))
                torrent_hash = _text(getattr(record, "download_hash", None)).lower()
                dest = _text(getattr(record, "dest", None))
                if downloader and torrent_hash and dest:
                    rows.append((downloader, torrent_hash, dest))
        except Exception as err:  # noqa: BLE001 - 历史兜底失败只降级，不影响主流程
            logger.warning(f"媒体同步守护读取 MoviePilot 整理历史失败（忽略历史兜底）：{err}")
            rows = []
        self._history_rows_cache = rows
        self._history_cache_at = now
        return rows

    # ==================================================================
    # 待执行动作执行
    # ==================================================================
    def _execute_due_actions(self, now: float) -> int:
        """执行所有已到期的待办动作，返回执行成功的目标数量。"""
        # 执行安全阀：插件未启用/停止中/本轮数据覆盖不完整时，一律不执行真实删除
        if self._stopping or not getattr(self._config, "enabled", False):
            return 0
        if not getattr(self, "_coverage_complete", False):
            return 0
        with self._lock:
            due_actions = [
                action
                for action in self._pending
                if action.is_pending and safe_float(action.due_at) <= now
            ]
        if not due_actions:
            return 0

        state_module, linker_module, guard_module = _core_modules()
        del state_module, linker_module  # 仅为统一延迟导入
        executed = 0
        executed_torrents = 0
        executed_media = 0
        removed_ids: set = set()
        exec_config = getattr(self, "_exec_config", self._config)
        for action in list(self._pending):
            if not action.is_pending:
                removed_ids.add(action.action_id)
                continue
            if safe_float(action.due_at) > now:
                continue
            if action.dry_run:
                action.results = [{"status": "skipped", "message": "演练模式，未执行删除"}]
                self._finish_action(action, "skipped", "演练模式，未执行删除（dry_run）")
                removed_ids.add(action.action_id)
                continue
            if getattr(exec_config, "dry_run", False):
                # 全局演练模式：真实动作保留，等待用户关闭演练
                continue
            last_coverage = getattr(self, "_last_coverage", {}) or {}
            if action.kind == "delete_torrents":
                if not getattr(exec_config, "auto_media_to_torrent", False):
                    continue
                # 没有可用的媒体数据源时，不能删种子（无法证明媒体真的已消失）
                if not last_coverage.get("media_ok"):
                    continue
            if action.kind == "delete_media":
                if not getattr(exec_config, "auto_torrent_to_media", False):
                    continue
                # 没有可用的下载器数据源时，不能删媒体（无法证明已无种子引用）
                if not last_coverage.get("torrent_ok"):
                    continue
            if action.kind == "delete_media" and not getattr(
                self, "_downloaders_verifiable", False
            ):
                # 下载器路径不可见/覆盖不完整：无法证明没有其它现存种子引用媒体文件
                continue

            try:
                if action.kind == "delete_torrents":
                    ok, reason = guard_module.recheck_torrent_action(
                        action, self._snapshot, self._links, exec_config
                    )
                elif action.kind == "delete_media":
                    ok, reason = guard_module.recheck_media_action(
                        action, self._snapshot, self._links, exec_config
                    )
                else:
                    ok, reason = False, f"未知动作类型：{action.kind}"
            except Exception as err:  # noqa: BLE001
                ok, reason = False, f"执行前复核异常：{err}"
            if not ok:
                action.results = [{"status": "skipped", "message": str(reason)}]
                self._finish_action(action, "skipped", f"执行前复核未通过：{reason}")
                removed_ids.add(action.action_id)
                continue

            deleted = 0
            failures: List[str] = []
            cleanup_warnings: List[str] = []
            try:
                if action.kind == "delete_torrents":
                    deleted, failures, cleanup_warnings = self._execute_torrent_action(action)
                elif action.kind == "delete_media":
                    deleted, failures, cleanup_warnings = self._execute_media_action(action)
            except Exception as err:  # noqa: BLE001 - 单条动作失败不影响其它动作
                failures = [f"执行异常：{err}"]
                logger.error(f"媒体同步守护执行动作 {action.action_id} 失败：{err}")

            summary_text = self._action_summary(action.kind, deleted, failures, cleanup_warnings)
            action.results = [
                {"status": "done", "deleted": deleted},
                *({"status": "failed", "message": message} for message in failures),
                *({"status": "warning", "message": message} for message in cleanup_warnings),
            ]
            status = "done" if deleted > 0 else "failed"
            self._finish_action(action, status, summary_text)
            removed_ids.add(action.action_id)
            executed += deleted
            if deleted > 0:
                if action.kind == "delete_torrents":
                    executed_torrents += deleted
                else:
                    executed_media += deleted

        with self._lock:
            # 只按 action_id 移除本函数已处理的动作，绝不覆盖整个 pending 列表，
            # 避免把执行期间被用户取消/新增的动作“复活”或丢失。
            self._pending = [
                item for item in self._pending if item.action_id not in removed_ids
            ]
        if executed_torrents or executed_media:
            self._notify(
                "媒体同步守护",
                f"媒体同步守护：已删除 {executed_torrents} 个种子 / "
                f"{executed_media} 个媒体条目。",
            )
            self._persist(force=True)
        return executed

    def _action_summary(
        self,
        kind: str,
        deleted: int,
        failures: List[str],
        warnings: List[str],
    ) -> str:
        """生成动作执行摘要（中文）。"""
        target = "种子" if kind == "delete_torrents" else "媒体条目"
        if deleted > 0:
            text = f"已删除 {deleted} 个{target}"
        else:
            text = f"未删除任何{target}"
        if failures:
            text += f"，{len(failures)} 个失败"
        if warnings:
            text += f"，{len(warnings)} 条告警"
        return text

    def _execute_torrent_action(
        self, action: PendingAction
    ) -> Tuple[int, List[str], List[str]]:
        """执行 delete_torrents 动作，返回 ``(成功数, 失败列表, 告警列表)``。"""
        state_module, linker_module, guard_module = _core_modules()
        del state_module, linker_module
        entries = [
            entry
            for entry in (action.payload or {}).get("torrents") or []
            if isinstance(entry, dict)
        ]
        failures: List[str] = []
        warnings: List[str] = []
        groups: Dict[Tuple[str, bool], List[str]] = {}
        deleted = 0

        for entry in entries:
            downloader = _text(entry.get("downloader"))
            torrent_hash = _text(entry.get("hash")).lower()
            if not downloader or not torrent_hash:
                failures.append("动作目标缺少下载器或 hash")
                continue
            key = torrent_key(downloader, torrent_hash)
            requested = bool(entry.get("delete_files", False))
            if requested and not getattr(self, "_file_deletion_safe", False):
                warnings.append(
                    f"{torrent_hash} 因下载器共享关系无法校验，已保留下载文件"
                )
                requested = False
            safe = False
            if requested:
                try:
                    safe = bool(guard_module.safe_delete_files_map([key], self._links).get(key, False))
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"媒体同步守护校验删文件安全性失败：{err}")
                    safe = False
                if not safe:
                    warnings.append(f"{torrent_hash} 与其它种子共享文件，已保留下载文件")
            groups.setdefault((downloader, bool(requested and safe)), []).append(torrent_hash)

        for (downloader, delete_files), hashes in groups.items():
            api = self._find_qbittorrent(downloader)
            if api is None:
                failures.append(f"下载器「{downloader}」不可用，无法删除种子")
                continue
            try:
                ok, message = api.delete_torrents(hashes, delete_files)
            except Exception as err:  # noqa: BLE001
                failures.append(f"下载器「{downloader}」删除种子异常：{err}")
                continue
            if not ok:
                failures.append(f"下载器「{downloader}」删除种子失败：{message}")
                continue
            deleted += len(hashes)
            for torrent_hash in hashes:
                self._clean_transfer_history_by_hash(torrent_hash)
                self._clean_download_history_by_hash(torrent_hash)
            with self._lock:
                for torrent_hash in hashes:
                    self._snapshot.torrents.pop(torrent_key(downloader, torrent_hash), None)

        return deleted, failures, warnings

    def _execute_media_action(
        self, action: PendingAction
    ) -> Tuple[int, List[str], List[str]]:
        """执行 delete_media 动作，返回 ``(成功数, 失败列表, 告警列表)``。"""
        entries = [
            entry
            for entry in (action.payload or {}).get("items") or []
            if isinstance(entry, dict)
        ]
        failures: List[str] = []
        warnings: List[str] = []
        deleted = 0
        protected_paths = {
            item.path for item in self._snapshot.media.values() if item and item.path
        }
        for entry in entries:
            server = _text(entry.get("server"))
            item_id = _text(entry.get("item_id"))
            if not server or not item_id:
                failures.append("动作目标缺少媒体服务器或 item_id")
                continue
            api = self._find_jellyfin(server)
            if api is None:
                failures.append(f"媒体服务器「{server}」不可用，无法删除媒体")
                continue
            media = self._snapshot.media.get(media_key(server, item_id))
            media_item = media or MediaItem.from_dict(entry)
            # 删除前先规划附属文件清理（Jellyfin 删除条目后文件可能一并消失）
            try:
                cleanup_plan = plan_media_cleanup(
                    media_item,
                    self._config,
                    protected_video_paths=protected_paths - {media_item.path},
                )
            except Exception as err:  # noqa: BLE001 - 清理规划失败只影响附属清理
                cleanup_plan = {"sidecar_files": [], "remove_dirs": [], "skipped": [str(err)]}
            try:
                ok, message, _status_code = api.delete_item(item_id)
            except Exception as err:  # noqa: BLE001
                failures.append(f"{media_item.display_title or item_id}：删除异常 {err}")
                continue
            if not ok:
                failures.append(f"{media_item.display_title or item_id}：{message}")
                continue
            deleted += 1
            safe_plan = self._safe_cleanup_plan(cleanup_plan)
            if safe_plan:
                try:
                    result = apply_media_cleanup(safe_plan)
                except Exception as err:  # noqa: BLE001
                    warnings.append(f"清理附属文件失败：{err}")
                else:
                    if result.get("failed"):
                        warnings.append(
                            f"清理附属文件失败 {len(result['failed'])} 个（{media_item.display_title}）"
                        )
            self._clean_transfer_history_by_dest(media_item.path)
            with self._lock:
                self._snapshot.media.pop(media_key(server, item_id), None)
        return deleted, failures, warnings

    def _safe_cleanup_plan(self, plan: Any) -> Optional[Dict[str, Any]]:
        """对清理计划再做一次路径安全过滤。"""
        if not isinstance(plan, dict):
            return None
        safe: Dict[str, Any] = {"sidecar_files": [], "remove_dirs": []}
        for path in plan.get("sidecar_files") or []:
            if is_safe_delete_target(path, self._config):
                safe["sidecar_files"].append(path)
        for path in plan.get("remove_dirs") or []:
            if is_safe_delete_target(path, self._config):
                safe["remove_dirs"].append(path)
        if not safe["sidecar_files"] and not safe["remove_dirs"]:
            return None
        return safe

    def _finish_action(self, action: PendingAction, status: str, summary: str) -> None:
        """把动作标记为终态并写入内存历史。"""
        action.status = status
        self._append_history(
            {
                "action_id": action.action_id,
                "kind": action.kind,
                "status": status,
                "finished_at": _now(),
                "summary": summary,
            }
        )

    def _append_history(self, entry: Dict[str, Any]) -> None:
        """追加动作历史，按 ``keep_history_actions`` 截断。"""
        self._history.append(entry)
        try:
            keep = int(getattr(self._config, "keep_history_actions", 50) or 0)
        except (TypeError, ValueError):
            keep = 50
        keep = max(0, keep)
        if keep and len(self._history) > keep:
            self._history = self._history[-keep:]
        elif not keep:
            self._history = []

    def _drop_dry_run_actions(self, summary: str) -> None:
        """丢弃所有 pending 的演练动作（dry_run 关闭时调用）。"""
        keep: List[PendingAction] = []
        for action in self._pending:
            if action.is_pending and action.dry_run:
                action.status = "cancelled"
                self._append_history(
                    {
                        "action_id": action.action_id,
                        "kind": action.kind,
                        "status": "cancelled",
                        "finished_at": _now(),
                        "summary": summary,
                    }
                )
            else:
                keep.append(action)
        self._pending = keep

    # ==================================================================
    # MoviePilot 历史记录清理（可选，全部 try/except）
    # ==================================================================
    def _clean_transfer_history_by_hash(self, torrent_hash: str) -> None:
        """按种子 hash 清理转移历史。"""
        if not getattr(self._config, "clean_moviepilot_history", False):
            return
        target = _text(torrent_hash).lower()
        if not target:
            return
        try:
            from app.db.transferhistory_oper import TransferHistoryOper

            oper = TransferHistoryOper()
            records = oper.list_by_hash(target) or []
            removed = 0
            for record in records:
                history_id = getattr(record, "id", None)
                if history_id is None:
                    continue
                try:
                    oper.delete(history_id)
                except Exception as err:  # noqa: BLE001 - 单条失败不影响其它
                    logger.warning(f"媒体同步守护删除转移历史失败（id={history_id}）：{err}")
                    continue
                removed += 1
            if removed:
                logger.info(f"媒体同步守护已清理 {removed} 条转移历史（hash={target}）")
        except Exception as err:  # noqa: BLE001 - DB 操作失败只记日志
            logger.warning(f"媒体同步守护清理转移历史失败（hash={target}）：{err}")

    def _clean_download_history_by_hash(self, torrent_hash: str) -> None:
        """按种子 hash 清理下载历史。"""
        if not getattr(self._config, "clean_moviepilot_history", False):
            return
        target = _text(torrent_hash).lower()
        if not target:
            return
        try:
            from app.db.downloadhistory_oper import DownloadHistoryOper

            oper = DownloadHistoryOper()
            record = oper.get_by_hash(target)
            history_id = getattr(record, "id", None) if record is not None else None
            if history_id is None:
                return
            oper.delete_history(history_id)
            logger.info(f"媒体同步守护已清理下载历史（hash={target}）")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"媒体同步守护清理下载历史失败（hash={target}）：{err}")

    def _clean_transfer_history_by_dest(self, path: str) -> None:
        """按媒体目标路径清理转移历史。"""
        if not getattr(self._config, "clean_moviepilot_history", False):
            return
        target = _text(path)
        if not target:
            return
        try:
            from app.db.transferhistory_oper import TransferHistoryOper

            oper = TransferHistoryOper()
            records: List[Any] = []
            try:
                single = oper.get_by_dest(dest=target)
            except TypeError:
                single = None
            except Exception as err:  # noqa: BLE001
                logger.warning(f"媒体同步守护查询转移历史失败（dest={target}）：{err}")
                single = None
            if isinstance(single, (list, tuple)):
                records.extend(single)
            elif single is not None:
                records.append(single)
            # get_by(dest=...) 在部分 MoviePilot 版本返回列表，作为兜底
            try:
                extra = oper.get_by(dest=target) or []
            except TypeError:
                extra = []
            except Exception:  # noqa: BLE001
                extra = []
            if isinstance(extra, (list, tuple)):
                records.extend(extra)
            removed = 0
            seen_ids = set()
            for record in records:
                history_id = getattr(record, "id", None)
                if history_id is None or history_id in seen_ids:
                    continue
                seen_ids.add(history_id)
                try:
                    oper.delete(history_id)
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"媒体同步守护删除转移历史失败（id={history_id}）：{err}")
                    continue
                removed += 1
            if removed:
                logger.info(f"媒体同步守护已清理 {removed} 条转移历史（dest={target}）")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"媒体同步守护清理转移历史失败（dest={target}）：{err}")

    # ==================================================================
    # 一次性扫描
    # ==================================================================
    def _empty_scan_state(self) -> Dict[str, Any]:
        return {
            "running": False,
            "started_at": 0.0,
            "finished_at": 0.0,
            "duration": 0.0,
            "progress": "",
            "error": "",
            "result": None,
        }

    def start_scan(self) -> Dict[str, Any]:
        """后台线程启动一次全量扫描。"""
        with self._scan_lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return {"started": False, "message": "扫描正在进行中"}
            if not self._clients or not self._configured:
                message = "；".join(self._warnings) or "尚未配置可用的 Jellyfin / qBittorrent 服务"
                return {"started": False, "message": message}
            self._scan_cancel_event = threading.Event()
            self._scan_state = {
                "running": True,
                "started_at": _now(),
                "finished_at": 0.0,
                "duration": 0.0,
                "progress": "正在读取媒体库…",
                "error": "",
                "result": None,
            }
            thread = threading.Thread(target=self._scan_worker, name="MediaSyncGuardScan", daemon=True)
            self._scan_thread = thread
        thread.start()
        return {"started": True, "message": "已开始扫描"}

    def cancel_scan(self) -> None:
        """请求取消正在进行的扫描（后台线程会在检查点退出）。"""
        self._scan_cancel_event.set()

    def _set_scan_progress(self, progress: str) -> None:
        with self._scan_lock:
            if self._scan_state.get("running"):
                self._scan_state["progress"] = progress

    def _finalize_scan(
        self,
        *,
        progress: str,
        error: str = "",
        result: Optional[Dict[str, Any]] = None,
        duration: float = 0.0,
    ) -> None:
        with self._scan_lock:
            started_at = safe_float(self._scan_state.get("started_at"))
            self._scan_state = {
                "running": False,
                "started_at": started_at,
                "finished_at": _now(),
                "duration": round(max(0.0, duration), 3),
                "progress": progress,
                "error": error,
                "result": result,
            }

    def _scan_cancelled(self) -> bool:
        return self._scan_cancel_event.is_set()

    def _scan_worker(self) -> None:
        """后台扫描线程主体：抓取 -> 建索引 -> 生成扫描结果。"""
        started_mono = time.monotonic()
        try:
            state_module, linker_module, guard_module = _core_modules()
            del state_module
            bundle = self._clients
            if bundle is None:
                self._finalize_scan(progress="失败", error="尚未配置可用的服务客户端")
                return

            media: Dict[str, MediaItem] = {}
            torrents: Dict[str, TorrentItem] = {}
            errors: List[str] = []
            for api in bundle.jellyfin:
                if self._scan_cancelled():
                    self._finalize_scan(progress="已取消", duration=time.monotonic() - started_mono)
                    return
                self._set_scan_progress(f"正在读取媒体库：{api.name}")
                try:
                    items, _warnings = api.list_media_items()
                except Exception as err:  # noqa: BLE001
                    errors.append(f"Jellyfin「{api.name}」读取失败：{err}")
                    continue
                for item in items or []:
                    media[item.key] = item
            for api in bundle.qbittorrent:
                if self._scan_cancelled():
                    self._finalize_scan(progress="已取消", duration=time.monotonic() - started_mono)
                    return
                self._set_scan_progress(f"正在读取下载任务：{api.name}")
                try:
                    items, _warnings = api.list_torrents()
                except Exception as err:  # noqa: BLE001
                    errors.append(f"qBittorrent「{api.name}」读取失败：{err}")
                    continue
                for item in items or []:
                    torrents[item.key] = item

            if errors:
                # 扫描结果用于人工勾选删除，任何来源失败都不产出“看似完整”的结果
                self._finalize_scan(
                    progress="失败",
                    error="；".join(errors[:5]),
                    duration=time.monotonic() - started_mono,
                )
                return
            if self._scan_cancelled():
                self._finalize_scan(progress="已取消", duration=time.monotonic() - started_mono)
                return

            snapshot = Snapshot(media=media, torrents=torrents)
            self._set_scan_progress("正在分析硬链接关系…")
            history_rows: List[Tuple[str, str, str]] = []
            try:
                history_rows = self._read_history_rows()
            except Exception as err:  # noqa: BLE001
                logger.warning(f"媒体同步守护扫描读取整理历史失败（忽略兜底）：{err}")
            build = linker_module.LinkBuilder().build(
                list(snapshot.media.values()), list(snapshot.torrents.values()), history_rows
            )
            if self._scan_cancelled():
                self._finalize_scan(progress="已取消", duration=time.monotonic() - started_mono)
                return

            self._set_scan_progress("正在生成扫描结果…")
            finished_at = _now()
            duration = time.monotonic() - started_mono
            result = guard_module.build_scan(
                self._config,
                snapshot,
                build.index,
                history_rows=history_rows,
                now=finished_at,
                stats=build.stats,
                duration=duration,
            )
            self._scan_links = build.index
            self._scan_snapshot = snapshot
            self._last_scan_summary = {
                "generated_at": safe_float((result or {}).get("generated_at")),
                "duration": safe_float((result or {}).get("duration")),
                "stats": dict((result or {}).get("stats") or {}),
            }
            self._finalize_scan(progress="完成", result=result, duration=duration)
            self._persist()
        except Exception as err:  # noqa: BLE001 - 扫描线程异常必须转成状态
            logger.error(f"媒体同步守护扫描失败：{err}")
            self._finalize_scan(
                progress="失败",
                error=str(err),
                duration=time.monotonic() - started_mono,
            )

    def get_scan_state(self) -> Dict[str, Any]:
        """契约 §5.2：扫描状态与结果（result 为 build_scan 的内层结构）。"""
        with self._scan_lock:
            state = dict(self._scan_state)
            running = bool(
                self._scan_thread is not None and self._scan_thread.is_alive()
            ) or bool(state.get("running"))
        result = state.get("result")
        return {
            "running": bool(running and state.get("result") is None),
            "started_at": safe_float(state.get("started_at")),
            "finished_at": safe_float(state.get("finished_at")),
            "duration": safe_float(state.get("duration")),
            "progress": _text(state.get("progress")),
            "error": _text(state.get("error")),
            "result": result,
        }

    # ==================================================================
    # 手动应用扫描选择
    # ==================================================================
    def apply_scan_selection(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按扫描结果手动删除选中媒体/种子（契约 §5.3）。"""
        payload = payload if isinstance(payload, dict) else {}
        kind = _text(payload.get("kind")).lower()
        if kind in {"delete_torrents", "torrent"}:
            kind = "torrents"
        elif kind in {"delete_media", "media_items"}:
            kind = "media"
        if kind not in {"media", "torrents"}:
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": ["未知的操作类型，仅支持 media / torrents"],
                "message": "未知的操作类型",
            }
        ids = [str(item).strip() for item in (payload.get("ids") or []) if str(item).strip()]
        if not ids:
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": "未选择任何条目",
            }

        if not getattr(self._config, "enabled", False):
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": "插件未启用，已拒绝手动删除（请在配置中启用）",
            }
        if len(ids) > MAX_MANUAL_APPLY_ITEMS:
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": f"单次最多允许删除 {MAX_MANUAL_APPLY_ITEMS} 项，请分批操作",
            }
        with self._scan_lock:
            result = self._scan_state.get("result")
        if not isinstance(result, dict):
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": "尚无扫描结果，请先执行扫描",
            }
        scan_generated = safe_float(result.get("generated_at"))
        if not scan_generated or (_now() - scan_generated) > MAX_SCAN_AGE_SECONDS:
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": "扫描结果已过期，请重新扫描后再删除",
            }
        # 手动删除前先同步刷新一次实时状态：扫描结果可能是若干分钟前的，
        # 必须用最新的媒体/种子/inode 关系做共享校验，避免误删仍被引用的文件。
        last_poll_at = safe_float((self._last_poll or {}).get("at"))
        if (_now() - last_poll_at) > MANUAL_APPLY_REFRESH_SECONDS:
            try:
                self.poll_once()
            except Exception as err:  # noqa: BLE001
                return {
                    "success": False,
                    "deleted": 0,
                    "failed": [],
                    "warnings": [],
                    "message": f"刷新实时状态失败，已取消本次删除：{err}",
                }
        if not getattr(self, "_coverage_complete", False):
            return {
                "success": False,
                "deleted": 0,
                "failed": [],
                "warnings": [],
                "message": "实时数据不完整（服务读取失败或覆盖不全），已取消本次删除，请稍后重试",
            }
        links = self._links
        state_module, linker_module, guard_module = _core_modules()
        del state_module, linker_module

        if kind == "media":
            if not getattr(self, "_downloaders_verifiable", False):
                return {
                    "success": False,
                    "deleted": 0,
                    "failed": [],
                    "warnings": [],
                    "message": (
                        "存在数据目录不可见或未纳入监控的下载器，无法校验媒体文件是否仍被其它种子引用；"
                        "已拒绝手动删除媒体。请先解决下载器数据可见性，或仅清理种子任务。"
                    ),
                }
            return self._apply_media_ids(ids, payload, result, links, guard_module)
        return self._apply_torrent_ids(ids, payload, result, links, guard_module)

    def _apply_media_ids(
        self,
        ids: List[str],
        payload: Dict[str, Any],
        result: Dict[str, Any],
        links: LinkIndex,
        guard_module: Any,
    ) -> Dict[str, Any]:
        rows = {
            str(row.get("id")): row
            for row in result.get("media_without_torrent") or []
            if isinstance(row, dict) and row.get("id")
        }
        delete_sidecars = bool(
            payload.get("delete_sidecars", getattr(self._config, "delete_media_sidecars", True))
        )
        cleanup_dirs = bool(
            payload.get("cleanup_empty_dirs", getattr(self._config, "cleanup_empty_dirs", True))
        )
        config = self._config.merged(
            delete_media_sidecars=delete_sidecars,
            cleanup_empty_dirs=cleanup_dirs,
        )
        deleted = 0
        failed: List[Dict[str, str]] = []
        warnings: List[str] = []
        protected_paths = {
            item.path for item in self._snapshot.media.values() if item and item.path
        }
        deleted_ids: List[str] = []
        for item_id in ids:
            row = rows.get(item_id)
            if row is None:
                failed.append({"id": item_id, "error": "扫描结果中不存在该条目"})
                continue
            server = _text(row.get("server"))
            jellyfin_item_id = _text(row.get("item_id"))
            path = _text(row.get("path"))
            inode = safe_int(row.get("inode"))
            dev = safe_int(row.get("dev"))
            if not server or not jellyfin_item_id:
                failed.append({"id": item_id, "error": "条目缺少媒体服务器或 item_id"})
                continue
            if not inode:
                # 无法校验 inode 的媒体条目保守拒绝，避免误删被种子引用的文件
                failed.append({"id": item_id, "error": "无法校验媒体文件 inode，已跳过"})
                continue
            live_media = self._snapshot.media.get(media_key(server, jellyfin_item_id))
            if live_media is None:
                failed.append({"id": item_id, "error": "媒体条目已不存在或已变更，请重新扫描"})
                continue
            if not live_media.inode:
                # M-C：媒体文件当前无法 stat（存储异常/文件消失但 Jellyfin 仍列条目），
                # 无法校验共享关系，拒绝删除。
                failed.append(
                    {"id": item_id, "error": "当前无法校验媒体文件 inode（存储异常或文件不存在），已跳过"}
                )
                continue
            if (live_media.inode != inode or live_media.dev != dev):
                failed.append({"id": item_id, "error": "媒体文件已变化（inode 不匹配），请重新扫描"})
                continue
            path = live_media.path or path
            try:
                still_shared = bool(links.current_torrents_of_inode(live_media.inode_key))
            except Exception:  # noqa: BLE001
                still_shared = True
            if still_shared:
                failed.append({"id": item_id, "error": "媒体文件仍被现存种子引用，已跳过"})
                warnings.append(f"{path or jellyfin_item_id} 仍被现存种子引用，已跳过")
                continue
            if not path or not is_safe_delete_target(path, config):
                failed.append({"id": item_id, "error": "媒体路径不在允许删除的目录内"})
                continue
            api = self._find_jellyfin(server)
            if api is None:
                failed.append({"id": item_id, "error": f"媒体服务器「{server}」不可用"})
                continue
            media_item = live_media
            try:
                cleanup_plan = plan_media_cleanup(
                    media_item,
                    config,
                    protected_video_paths=protected_paths - {path},
                )
            except Exception as err:  # noqa: BLE001
                cleanup_plan = {"sidecar_files": [], "remove_dirs": [], "skipped": [str(err)]}
            ok, message, _status = api.delete_item(jellyfin_item_id)
            if not ok:
                failed.append({"id": item_id, "error": message})
                continue
            deleted += 1
            deleted_ids.append(item_id)
            safe_plan = self._safe_cleanup_plan(cleanup_plan)
            if safe_plan:
                apply_media_cleanup(safe_plan)
            self._clean_transfer_history_by_dest(path)
        if deleted_ids:
            with self._lock:
                for item_id in deleted_ids:
                    row = rows.get(item_id) or {}
                    self._snapshot.media.pop(
                        media_key(_text(row.get("server")), _text(row.get("item_id"))), None
                    )
            self._remove_scan_rows("media_without_torrent", deleted_ids)
            self._links_signature = None
            self._persist(force=True)
        message = f"已删除 {deleted} 个媒体条目" if deleted else "未删除任何媒体条目"
        if deleted:
            self._notify("媒体同步守护", f"媒体同步守护：{message}。")
        return {
            "success": deleted > 0,
            "deleted": deleted,
            "failed": failed,
            "warnings": warnings,
            "message": message,
        }

    def _apply_torrent_ids(
        self,
        ids: List[str],
        payload: Dict[str, Any],
        result: Dict[str, Any],
        links: LinkIndex,
        guard_module: Any,
    ) -> Dict[str, Any]:
        rows = {
            str(row.get("id")): row
            for row in result.get("torrents_without_media") or []
            if isinstance(row, dict) and row.get("id")
        }
        request_delete_files = bool(payload.get("delete_files", False))
        deleted = 0
        failed: List[Dict[str, str]] = []
        warnings: List[str] = []
        deleted_ids: List[str] = []
        for item_id in ids:
            row = rows.get(item_id)
            if row is None:
                failed.append({"id": item_id, "error": "扫描结果中不存在该条目"})
                continue
            downloader = _text(row.get("downloader"))
            torrent_hash = _text(row.get("hash")).lower()
            if not downloader or not torrent_hash:
                failed.append({"id": item_id, "error": "条目缺少下载器或 hash"})
                continue
            if _text(row.get("ignored_reason")):
                failed.append({"id": item_id, "error": "该种子命中忽略类目/标签，禁止删除"})
                warnings.append(f"{torrent_hash} 命中忽略规则，已跳过")
                continue
            key = torrent_key(downloader, torrent_hash)
            live_torrent = self._snapshot.torrents.get(key)
            if live_torrent is None:
                failed.append({"id": item_id, "error": "种子已不存在，请重新扫描"})
                continue
            try:
                recheck_ignored = guard_module.is_ignored(live_torrent, self._config)
            except Exception:  # noqa: BLE001
                recheck_ignored = None
            if recheck_ignored:
                failed.append({"id": item_id, "error": "该种子当前命中忽略类目/标签，禁止删除"})
                warnings.append(f"{torrent_hash} 当前命中忽略规则，已跳过")
                continue
            if links.media_of(key):
                failed.append({"id": item_id, "error": "该种子当前仍有媒体关联，请重新扫描"})
                warnings.append(f"{torrent_hash} 当前仍有媒体关联，已跳过")
                continue
            delete_files = request_delete_files
            if delete_files and not getattr(self, "_file_deletion_safe", False):
                delete_files = False
                warnings.append(f"{torrent_hash} 因下载器共享关系无法校验，已保留下载文件")
            if delete_files:
                try:
                    safe = bool(guard_module.safe_delete_files_map([key], links).get(key, False))
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"媒体同步守护校验删文件安全性失败：{err}")
                    safe = False
                if not safe:
                    delete_files = False
                    warnings.append(f"{torrent_hash} 与其它种子共享文件，已保留下载文件")
            api = self._find_qbittorrent(downloader)
            if api is None:
                failed.append({"id": item_id, "error": f"下载器「{downloader}」不可用"})
                continue
            ok, message = api.delete_torrents([torrent_hash], delete_files)
            if not ok:
                failed.append({"id": item_id, "error": message})
                continue
            deleted += 1
            deleted_ids.append(item_id)
            self._clean_transfer_history_by_hash(torrent_hash)
            self._clean_download_history_by_hash(torrent_hash)
        if deleted_ids:
            with self._lock:
                for item_id in deleted_ids:
                    row = rows.get(item_id) or {}
                    self._snapshot.torrents.pop(
                        torrent_key(_text(row.get("downloader")), _text(row.get("hash"))), None
                    )
            self._remove_scan_rows("torrents_without_media", deleted_ids)
            self._links_signature = None
            self._persist(force=True)
        message = f"已删除 {deleted} 个下载任务" if deleted else "未删除任何下载任务"
        if deleted:
            self._notify("媒体同步守护", f"媒体同步守护：{message}。")
        return {
            "success": deleted > 0,
            "deleted": deleted,
            "failed": failed,
            "warnings": warnings,
            "message": message,
        }

    def _remove_scan_rows(self, key: str, removed_ids: List[str]) -> None:
        """从最近一次扫描结果中移除已成功删除的行。"""
        if not removed_ids:
            return
        with self._scan_lock:
            result = self._scan_state.get("result")
            if not isinstance(result, dict):
                return
            rows = [row for row in result.get(key) or [] if str(row.get("id")) not in set(removed_ids)]
            result[key] = rows
            # 同步过滤分组中已空的 item_ids
            group_key = "media_groups" if key == "media_without_torrent" else "torrent_groups"
            groups = []
            for group in result.get(group_key) or []:
                if not isinstance(group, dict):
                    continue
                item_ids = group.get("item_ids")
                if isinstance(item_ids, list):
                    remaining = [item for item in item_ids if str(item) not in set(removed_ids)]
                    if not remaining:
                        continue
                    group = dict(group)
                    group["item_ids"] = remaining
                    group["count"] = len(remaining)
                groups.append(group)
            result[group_key] = groups

    # ==================================================================
    # 对外状态 / 待办
    # ==================================================================
    def get_status(self) -> Dict[str, Any]:
        """契约 §5.1：插件状态总览（始终返回 dict，不抛异常）。"""
        try:
            with self._lock:
                selected_downloaders = (
                    [api.name for api in self._clients.qbittorrent]
                    if self._clients and self._clients.qbittorrent
                    else list(self._config.downloaders or [])
                )
                selected_media_servers = (
                    [api.name for api in self._clients.jellyfin]
                    if self._clients and self._clients.jellyfin
                    else list(self._config.media_servers or [])
                )
                pending = [action for action in self._pending if action.is_pending]
                next_due_at = min(
                    (safe_float(action.due_at) for action in pending if safe_float(action.due_at) > 0),
                    default=0.0,
                )
                candidates = {
                    "torrents": list(self._candidates.get("torrents") or []),
                    "media": list(self._candidates.get("media") or []),
                }
                last_poll = dict(self._last_poll or self._default_last_poll(0.0))
                last_error = self._last_error
                warnings = self._dedupe(
                    list(self._warnings) + list(self._coverage_warnings)
                )
                configured = bool(self._configured)
                coverage = {
                    "complete": bool(self._coverage_complete),
                    "downloaders_verifiable": bool(self._downloaders_verifiable),
                    "media_to_torrent_verifiable": bool(self._media_to_torrent_verifiable),
                    "file_deletion_safe": bool(self._file_deletion_safe),
                    "expected_downloaders": sorted(self._expected_downloaders),
                    "expected_media_servers": sorted(self._expected_media_servers),
                    "enabled_downloaders": sorted(self._enabled_downloaders),
                    "configured_downloaders": sorted(self._configured_downloaders),
                    "enabled_media_servers": sorted(self._enabled_media_servers),
                }
            with self._scan_lock:
                result = self._scan_state.get("result")
                running = bool(
                    self._scan_thread is not None and self._scan_thread.is_alive()
                ) or bool(self._scan_state.get("running"))
            last_generated_at = (
                safe_float((result or {}).get("generated_at")) if isinstance(result, dict) else 0.0
            )
            return {
                "enabled": bool(getattr(self._config, "enabled", False)),
                "dry_run": bool(getattr(self._config, "dry_run", True)),
                "configured": configured,
                "warnings": warnings,
                "selected_downloaders": selected_downloaders,
                "selected_media_servers": selected_media_servers,
                "auto": {
                    "media_to_torrent": bool(
                        getattr(self._config, "auto_media_to_torrent", False)
                    ),
                    "torrent_to_media": bool(
                        getattr(self._config, "auto_torrent_to_media", False)
                    ),
                },
                "last_poll": last_poll,
                "pending": {
                    "count": len(pending),
                    "dry_run_count": sum(1 for action in pending if action.dry_run),
                    "next_due_at": next_due_at,
                },
                "candidates": candidates,
                "coverage": coverage,
                "scan": {
                    "running": bool(running and result is None),
                    "has_result": isinstance(result, dict),
                    "last_generated_at": last_generated_at,
                },
                "last_error": last_error,
            }
        except Exception as err:  # noqa: BLE001 - 状态接口必须始终返回 dict
            logger.error(f"媒体同步守护生成状态失败：{err}")
            return {
                "enabled": bool(getattr(self._config, "enabled", False)),
                "dry_run": bool(getattr(self._config, "dry_run", True)),
                "configured": False,
                "warnings": [f"生成状态失败：{err}"],
                "selected_downloaders": [],
                "selected_media_servers": [],
                "auto": {"media_to_torrent": False, "torrent_to_media": False},
                "last_poll": self._default_last_poll(0.0),
                "pending": {"count": 0, "dry_run_count": 0, "next_due_at": 0.0},
                "candidates": {"torrents": [], "media": []},
                "scan": {"running": False, "has_result": False, "last_generated_at": 0.0},
                "last_error": str(err),
            }

    def _action_targets(self, action: PendingAction) -> List[str]:
        """动作目标键列表（供 UI 展示）。"""
        payload = action.payload or {}
        targets: List[str] = []
        if action.kind == "delete_torrents":
            for entry in payload.get("torrents") or []:
                if not isinstance(entry, dict):
                    continue
                key = torrent_key(_text(entry.get("downloader")), _text(entry.get("hash")))
                if key not in targets:
                    targets.append(key)
        elif action.kind == "delete_media":
            for entry in payload.get("items") or []:
                if not isinstance(entry, dict):
                    continue
                key = media_key(_text(entry.get("server")), _text(entry.get("item_id")))
                if key not in targets:
                    targets.append(key)
        return targets

    def get_pending(self) -> Dict[str, Any]:
        """契约 §5.4：待执行动作与历史。"""
        with self._lock:
            pending = []
            for action in self._pending:
                if not action.is_pending:
                    continue
                try:
                    data = action.to_dict()
                except Exception:  # noqa: BLE001
                    continue
                data["targets"] = self._action_targets(action)
                pending.append(data)
            history = list(self._history)
        return {"pending": pending, "history": history}

    def cancel_pending(self, action_id: Optional[str] = None) -> int:
        """取消待执行动作（指定 id 或全部），返回取消数量。"""
        target = _text(action_id)
        cancelled = 0
        with self._lock:
            keep: List[PendingAction] = []
            for action in self._pending:
                if not action.is_pending:
                    continue
                if target and action.action_id != target:
                    keep.append(action)
                    continue
                action.status = "cancelled"
                self._append_history(
                    {
                        "action_id": action.action_id,
                        "kind": action.kind,
                        "status": "cancelled",
                        "finished_at": _now(),
                        "summary": "用户取消",
                    }
                )
                cancelled += 1
            self._pending = keep
        if cancelled:
            self._persist(force=True)
        return cancelled

    def test_connections(self) -> Dict[str, Any]:
        """测试所有已配置 Jellyfin/qBittorrent 连接。"""
        results: List[Dict[str, Any]] = []
        clients = self._clients
        if clients is None:
            return {
                "success": False,
                "results": [],
                "message": "；".join(self._warnings) or "尚未配置可用的服务",
            }
        for api in clients.jellyfin or []:
            try:
                ok, message = api.test()
            except Exception as err:  # noqa: BLE001
                ok, message = False, str(err)
            results.append(
                {"service": api.name, "type": "jellyfin", "ok": bool(ok), "message": message}
            )
        for api in clients.qbittorrent or []:
            try:
                ok, message = api.test()
            except Exception as err:  # noqa: BLE001
                ok, message = False, str(err)
            results.append(
                {"service": api.name, "type": "qbittorrent", "ok": bool(ok), "message": message}
            )
        if not results:
            return {
                "success": False,
                "results": [],
                "message": "；".join(self._warnings) or "尚未配置可用的服务",
            }
        success = all(item["ok"] for item in results)
        if success:
            message = "全部服务连接正常"
        elif any(item["ok"] for item in results):
            message = "部分服务连接失败，请检查插件配置"
        else:
            message = "所有服务连接失败，请检查插件配置"
        return {"success": success, "results": results, "message": message}
