"""MoviePilot（``app.*``）最小桩模块，供插件在 MoviePilot 进程外导入。

仅用于单元测试，不参与运行时。参考 ``plugins.v2/aisubv2/tests/test_plugin_config.py``
的做法，在导入插件之前安装到 ``sys.modules``。
"""

from __future__ import annotations

import pathlib
import sys
import types
from typing import Any, Dict, List, Optional

LOG_RECORDS: List[tuple] = []
POSTED_MESSAGES: List[dict] = []


def new_module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


class StubLogger:
    def _append(self, level: str, message: Any) -> None:
        LOG_RECORDS.append((level, str(message)))

    def debug(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        self._append("debug", message)

    def info(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        self._append("info", message)

    def warning(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        self._append("warning", message)

    warn = warning

    def error(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        self._append("error", message)

    def exception(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        self._append("exception", message)


class StubNotificationType:
    Plugin = "Plugin"
    MediaServer = "MediaServer"
    Download = "Download"


class StubEventType:
    DownloadDeleted = "download.deleted"
    DownloadFileDeleted = "downloadfile.deleted"
    PluginReload = "plugin.reload"


class StubPluginBase:
    """最小 _PluginBase 桩。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._stub_data: Dict[str, Any] = {}
        self._stub_data_path: Optional[pathlib.Path] = None
        self._stub_updated_config: Optional[dict] = None

    def get_data(self, key: Optional[str] = None, default: Any = None, **kwargs: Any) -> Any:
        if key in self._stub_data:
            return self._stub_data[key]
        return {} if default is None else default

    def save_data(self, key: str, value: Any, **kwargs: Any) -> None:
        self._stub_data[key] = value

    def del_data(self, key: str, **kwargs: Any) -> None:
        self._stub_data.pop(key, None)

    def update_config(self, cfg: dict, **kwargs: Any) -> None:
        self._stub_updated_config = cfg

    def get_data_path(self) -> pathlib.Path:
        if self._stub_data_path is None:
            import tempfile

            self._stub_data_path = pathlib.Path(tempfile.mkdtemp(prefix="mediasyncguard-stub-"))
        return self._stub_data_path

    def post_message(self, **kwargs: Any) -> None:
        POSTED_MESSAGES.append(kwargs)

    def get_config(self, key: str, **kwargs: Any) -> Any:
        return None


class StubRequestUtils:
    """MoviePilot RequestUtils 桩：只在测试需要时被替换，默认不发网络请求。"""

    last_url: Optional[str] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def get_res(self, url: str = "", *args: Any, **kwargs: Any) -> Any:
        StubRequestUtils.last_url = url
        raise RuntimeError(f"RequestUtils stub 不支持真实请求：{url}")

    def post_res(self, url: str = "", *args: Any, **kwargs: Any) -> Any:
        StubRequestUtils.last_url = url
        raise RuntimeError(f"RequestUtils stub 不支持真实请求：{url}")

    def delete_res(self, url: str = "", *args: Any, **kwargs: Any) -> Any:
        StubRequestUtils.last_url = url
        raise RuntimeError(f"RequestUtils stub 不支持真实请求：{url}")


class StubDownloaderHelper:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_configs(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    def get_service(self, name: str = "", *args: Any, **kwargs: Any) -> Any:
        return None

    def get_services(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {}

    def is_downloader(self, *args: Any, **kwargs: Any) -> bool:
        return False


class StubMediaServerHelper(StubDownloaderHelper):
    def is_media_server(self, *args: Any, **kwargs: Any) -> bool:
        return False


class StubTransferHistoryOper:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_by_hash(self, download_hash: str) -> List[Any]:
        return []

    def get_by_dest(self, dest: str, *args: Any, **kwargs: Any) -> List[Any]:
        return []

    def get_by(self, **kwargs: Any) -> List[Any]:
        return []

    def delete(self, historyid: Any) -> None:
        return None


class StubDownloadHistoryOper:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_by_hash(self, download_hash: str) -> List[Any]:
        return []

    def get_by_hashes(self, hashes: List[str]) -> Dict[str, Any]:
        return {}

    def delete(self, historyid: Any) -> None:
        return None

    def get_files_by_hash(self, download_hash: str) -> List[Any]:
        return []


def install_stubs() -> None:
    """安装所有需要的 app.* 桩模块（幂等）。"""
    if getattr(install_stubs, "_installed", False):
        return
    install_stubs._installed = True  # type: ignore[attr-defined]

    app_pkg = new_module("app")
    core_pkg = new_module("app.core")
    schemas_pkg = new_module("app.schemas")
    utils_pkg = new_module("app.utils")
    db_pkg = new_module("app.db")
    helper_pkg = new_module("app.helper")
    modules_pkg = new_module("app.modules")
    app_pkg.core = core_pkg
    app_pkg.schemas = schemas_pkg
    app_pkg.utils = utils_pkg
    app_pkg.db = db_pkg
    app_pkg.helper = helper_pkg
    app_pkg.modules = modules_pkg

    class _Settings:
        RMT_MEDIAEXT = {".mkv", ".mp4", ".avi", ".ts"}
        PROXY = {}
        TORRENT_TAG = ""
        DOWNLOAD_TMPEXT = [".!qB", ".part"]
        SUPERUSER = "admin"
        API_V1_STR = "/api/v1"
        PLUGIN_DATA_PATH = pathlib.Path("/tmp/mediasyncguard-plugins")
        ROOT_PATH = pathlib.Path("/tmp/mediasyncguard-root")
        VERSION = "v2.15.6"
        DEV = False
        PLUGIN_LOCAL_REPO_PATHS = None

    core_pkg.config = new_module("app.core.config", settings=_Settings())

    class _Event:
        def __init__(self, event_type: Any = None, event_data: Any = None) -> None:
            self.event_type = event_type
            self.event_data = event_data

    class _EventManager:
        def register(self, *args: Any, **kwargs: Any):
            def _decorator(func):
                return func

            return _decorator

        def send_event(self, *args: Any, **kwargs: Any) -> None:
            return None

        def enable_event_handler(self, *args: Any, **kwargs: Any) -> None:
            return None

        def disable_event_handler(self, *args: Any, **kwargs: Any) -> None:
            return None

    core_pkg.event = new_module("app.core.event", eventmanager=_EventManager(), Event=_Event)

    schemas_pkg.types = new_module(
        "app.schemas.types",
        NotificationType=StubNotificationType,
        EventType=StubEventType,
    )

    class StubMediaType:
        MOVIE = "电影"
        TV = "电视剧"

    schemas_pkg.MediaType = StubMediaType
    schemas_pkg.Notification = object
    schemas_pkg.NotificationType = StubNotificationType

    app_pkg.log = new_module("app.log", logger=StubLogger())

    utils_pkg.http = new_module("app.utils.http", RequestUtils=StubRequestUtils)
    utils_pkg.url = new_module("app.utils.url", UrlUtils=object)
    utils_pkg.system = new_module("app.utils.system", SystemUtils=object)

    db_pkg.transferhistory_oper = new_module(
        "app.db.transferhistory_oper", TransferHistoryOper=StubTransferHistoryOper
    )
    db_pkg.downloadhistory_oper = new_module(
        "app.db.downloadhistory_oper", DownloadHistoryOper=StubDownloadHistoryOper
    )

    helper_pkg.downloader = new_module(
        "app.helper.downloader", DownloaderHelper=StubDownloaderHelper
    )
    helper_pkg.mediaserver = new_module(
        "app.helper.mediaserver", MediaServerHelper=StubMediaServerHelper
    )
    helper_pkg.service = new_module("app.helper.service")

    app_pkg.plugins = new_module("app.plugins", _PluginBase=StubPluginBase)
