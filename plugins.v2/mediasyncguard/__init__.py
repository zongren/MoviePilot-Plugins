"""媒体同步守护（MediaSyncGuard）。

监控 Jellyfin 与 qBittorrent 的媒体/种子关系，实现受控的双向同步删除。
默认演练模式：只记录/通知候选项，自动删除开关默认关闭。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .config import PluginConfig
from .runtime import SyncRuntime


class MediaSyncGuard(_PluginBase):
    """媒体同步守护。"""

    # 插件名称
    plugin_name = "媒体同步守护"
    # 插件描述
    plugin_desc = (
        "监控 Jellyfin 与 qBittorrent，维护媒体文件与下载任务的硬链接关系；"
        "支持媒体删除联动删种、种子删除联动清理媒体，并提供一次性差异扫描与手动清理。"
    )
    # 插件图标
    plugin_icon = "mediasyncguard.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "TimoYoung"
    # 作者主页
    author_url = "https://github.com/TimoYoung/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediasyncguard_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别：站点认证用户可见
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._config = PluginConfig()
        self._runtime: Optional[SyncRuntime] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        """生效插件配置。"""
        self._config = PluginConfig.from_dict(config)
        try:
            if self._runtime is None:
                self._runtime = SyncRuntime(plugin=self, config=self._config)
            else:
                self._runtime.configure(self._config)
        except Exception as err:  # pragma: no cover - 防御性兜底
            logger.error(f"媒体同步守护初始化失败：{err}")

    def stop_service(self):
        """退出插件。"""
        try:
            if self._runtime:
                self._runtime.stop()
        except Exception as err:  # pragma: no cover
            logger.error(f"媒体同步守护退出失败：{err}")

    def get_state(self) -> bool:
        return bool(self._config.enabled)

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """配置页/数据页使用 Vue 联邦组件渲染。"""
        return "vue", "dist/assets"

    # ------------------------------------------------------------------
    # 定时任务
    # ------------------------------------------------------------------
    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时监控服务。"""
        if not self.get_state():
            return []
        return [
            {
                "id": "MediaSyncGuardMonitor",
                "name": "媒体同步守护监控",
                "trigger": "interval",
                "func": self.__scheduled_poll,
                "kwargs": {"seconds": self._config.effective_poll_seconds},
            }
        ]

    def __scheduled_poll(self) -> None:
        if not self._runtime:
            return
        try:
            self._runtime.scheduled_poll()
        except Exception as err:  # pragma: no cover
            logger.error(f"媒体同步守护定时监控出错：{err}")

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------
    def _service_select_items(self) -> Tuple[List[dict], List[dict], List[str]]:
        """动态生成下载器/媒体服务器选项，供配置页下拉框使用。"""
        downloader_items: List[dict] = []
        media_server_items: List[dict] = []
        warnings: List[str] = []
        try:
            from .clients import ServiceCatalog

            catalog = ServiceCatalog()
            for option in catalog.list_downloaders():
                suffix = "，默认" if option.default else ""
                downloader_items.append(
                    {"title": f"{option.name}（{option.type}{suffix}）", "value": option.name}
                )
            for option in catalog.list_media_servers():
                media_server_items.append(
                    {"title": f"{option.name}（{option.type}）", "value": option.name}
                )
        except Exception as err:
            logger.error(f"媒体同步守护读取下载器/媒体服务器配置失败：{err}")
            warnings.append("读取下载器/媒体服务器配置失败，请检查 MoviePilot 服务配置")
        if not downloader_items:
            warnings.append("未检测到已启用的 qBittorrent 下载器，请先在 MoviePilot 设置中配置")
        if not media_server_items:
            warnings.append("未检测到已启用的 Jellyfin 媒体服务器，请先在 MoviePilot 设置中配置")
        return downloader_items, media_server_items, warnings

    @staticmethod
    def _item_checkbox(component: str, model: str, label: str, hint: str = "", color: str = "primary") -> dict:
        props: Dict[str, Any] = {"model": model, "label": label, "color": color}
        if hint:
            props.update({"hint": hint, "persistent-hint": True})
        return {"component": component, "props": props}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面。"""
        downloader_items, media_server_items, warnings = self._service_select_items()
        warning_nodes = [
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mb-2",
                },
                "text": warning,
            }
            for warning in warnings
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "演练模式（仅记录/通知，不自动删除）",
                                            "color": "warning",
                                            "hint": "建议先保持开启，用扫描页确认关联关系后再关闭",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloaders",
                                            "label": "监控的下载器",
                                            "items": downloader_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "留空则使用 MoviePilot 默认下载器；副QB 数据目录若未挂载到 MoviePilot，将无法精确关联",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "media_servers",
                                            "label": "监控的媒体服务器",
                                            "items": media_server_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "留空则使用全部已启用的 Jellyfin 媒体服务器（当前仅支持 Jellyfin）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "poll_seconds",
                                            "label": "监控间隔（秒）",
                                            "type": "number",
                                            "min": 15,
                                            "max": 3600,
                                            "hint": "默认 60 秒；间隔越短越及时，但 API 调用越多",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "grace_seconds",
                                            "label": "自动删除延迟（秒）",
                                            "type": "number",
                                            "min": 0,
                                            "max": 86400,
                                            "hint": "检测到变化后等待该时间再执行，默认 600 秒；期间可在管理页取消",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "batch_limit_media",
                                            "label": "媒体批量保护阈值",
                                            "type": "number",
                                            "min": 0,
                                            "max": 10000,
                                            "hint": "单轮消失的媒体条目达到该数量时视为批量操作，跳过自动删种；0 表示不启用",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "batch_limit_torrents",
                                            "label": "种子批量保护阈值",
                                            "type": "number",
                                            "min": 0,
                                            "max": 10000,
                                            "hint": "单轮消失的种子达到该数量时视为自动清理/批量操作，跳过自动删媒体；0 表示不启用",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "media_path_map",
                                            "label": "媒体路径映射",
                                            "rows": 2,
                                            "placeholder": "/media:/medias",
                                            "hint": "每行一个“媒体服务器内前缀:MoviePilot 内前缀”，Jellyfin 默认 /media 对应 /medias",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "download_path_map",
                                            "label": "下载器补充路径映射（可选）",
                                            "rows": 2,
                                            "placeholder": "",
                                            "hint": "MoviePilot 下载器配置里的路径映射会自动生效；此处仅作额外兜底，每行一个“源:目标”",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mb-3",
                        },
                        "text": "自动删除默认关闭。请先在下方“扫描”管理页查看差异结果，确认关联关系后再开启自动执行；批量保护与延迟执行建议保持开启。",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_media_to_torrent",
                                            "label": "媒体删除 → 自动删除关联种子",
                                            "color": "warning",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_torrent_to_media",
                                            "label": "种子删除 → 自动删除关联媒体",
                                            "color": "warning",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_delete_files",
                                            "label": "联动删除时允许删除磁盘文件",
                                            "color": "warning",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ignore_categories",
                                            "label": "忽略的下载器分类",
                                            "placeholder": "KEEP",
                                            "hint": "逗号分隔；命中分类的种子不会被自动处理（默认 KEEP）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ignore_tags",
                                            "label": "忽略的下载器标签",
                                            "placeholder": "",
                                            "hint": "逗号分隔；命中标签的种子不会被自动处理",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_torrent_files",
                                            "label": "删种时删除下载文件（仅安全时）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_media_sidecars",
                                            "label": "删除媒体时清理附属文件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "cleanup_empty_dirs",
                                            "label": "清理因此产生的空目录",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_moviepilot_history",
                                            "label": "清理 MoviePilot 历史记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_include_unfinished",
                                            "label": "扫描包含未完成种子",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "media_delete_roots",
                                            "label": "允许删除的媒体根目录",
                                            "rows": 2,
                                            "hint": "每行一个；只有这些目录内的媒体文件/附属文件会被删除（安全阀）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "download_delete_roots",
                                            "label": "下载目录根",
                                            "rows": 2,
                                            "hint": "每行一个；用于区分“下载文件”与媒体文件，以及副QB 路径不可见检测",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": warning_nodes,
                            }
                        ],
                    },
                ],
            }
        ], self._config.to_dict()

    def get_page(self) -> List[dict]:
        """Vue 模式下数据页由前端联邦组件渲染，这里返回空列表作为旧版本回退。"""
        return []

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "插件运行状态",
            },
            {
                "path": "/options",
                "endpoint": self.api_options,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "可选下载器与媒体服务器",
            },
            {
                "path": "/scan",
                "endpoint": self.api_start_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "开始一次差异扫描",
            },
            {
                "path": "/scan/result",
                "endpoint": self.api_scan_result,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "扫描状态与结果",
            },
            {
                "path": "/scan/cancel",
                "endpoint": self.api_cancel_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取消扫描",
            },
            {
                "path": "/apply",
                "endpoint": self.api_apply,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按扫描结果手动删除选中项",
            },
            {
                "path": "/pending",
                "endpoint": self.api_pending,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "待执行动作与历史",
            },
            {
                "path": "/pending/cancel",
                "endpoint": self.api_cancel_pending,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "取消待执行动作",
            },
            {
                "path": "/poll",
                "endpoint": self.api_poll,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行一轮监控（演练模式下无删除）",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "测试下载器与媒体服务器连接",
            },
        ]

    @staticmethod
    def _not_ready() -> Dict[str, Any]:
        return {"success": False, "message": "插件尚未初始化，请保存配置或重新加载插件"}

    def api_status(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.get_status()
        except Exception as err:
            logger.error(f"媒体同步守护获取状态失败：{err}")
            return {"success": False, "message": f"获取状态失败：{err}"}

    def api_options(self) -> Dict[str, Any]:
        try:
            from .clients import ServiceCatalog

            catalog = ServiceCatalog()
            return {
                "downloaders": [
                    {
                        "name": option.name,
                        "type": option.type,
                        "default": option.default,
                        "enabled": option.enabled,
                    }
                    for option in catalog.list_downloaders()
                ],
                "media_servers": [
                    {
                        "name": option.name,
                        "type": option.type,
                        "enabled": option.enabled,
                        "host": option.host,
                    }
                    for option in catalog.list_media_servers()
                ],
            }
        except Exception as err:
            logger.error(f"媒体同步守护获取服务选项失败：{err}")
            return {"success": False, "message": f"获取服务选项失败：{err}"}

    def api_start_scan(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.start_scan()
        except Exception as err:
            logger.error(f"媒体同步守护启动扫描失败：{err}")
            return {"started": False, "message": f"启动扫描失败：{err}"}

    def api_scan_result(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.get_scan_state()
        except Exception as err:
            logger.error(f"媒体同步守护读取扫描结果失败：{err}")
            return {"running": False, "error": str(err), "result": None}

    def api_cancel_scan(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            self._runtime.cancel_scan()
            return {"success": True}
        except Exception as err:
            logger.error(f"媒体同步守护取消扫描失败：{err}")
            return {"success": False, "message": str(err)}

    def api_apply(self, payload: dict = None) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.apply_scan_selection(payload or {})
        except Exception as err:
            logger.error(f"媒体同步守护执行手动删除失败：{err}")
            return {"success": False, "message": f"执行失败：{err}"}

    def api_pending(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.get_pending()
        except Exception as err:
            logger.error(f"媒体同步守护读取待执行动作失败：{err}")
            return {"pending": [], "history": []}

    def api_cancel_pending(self, payload: dict = None) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            action_id = (payload or {}).get("action_id")
            count = self._runtime.cancel_pending(action_id=action_id)
            return {"success": True, "cancelled": count}
        except Exception as err:
            logger.error(f"媒体同步守护取消待执行动作失败：{err}")
            return {"success": False, "message": str(err)}

    def api_poll(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.trigger_poll()
        except Exception as err:
            logger.error(f"媒体同步守护手动轮询失败：{err}")
            return {"success": False, "message": str(err)}

    def api_test(self) -> Dict[str, Any]:
        if not self._runtime:
            return self._not_ready()
        try:
            return self._runtime.test_connections()
        except Exception as err:
            logger.error(f"媒体同步守护连接测试失败：{err}")
            return {"success": False, "message": str(err)}

    # ------------------------------------------------------------------
    # 命令/事件（保留扩展点）
    # ------------------------------------------------------------------
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _notify(self, title: str, text: str) -> None:
        """发送通知（供 runtime 调用）。"""
        if not self._config.notify:
            return
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:  # pragma: no cover
            logger.error(f"媒体同步守护发送通知失败：{err}")
