"""MediaSyncGuard MoviePilot 适配层（clients.py）单元测试。

全部使用假客户端 / 桩模块，不发起任何网络请求、不连接真实 Jellyfin/qBittorrent。

运行：``python3 plugins.v2/mediasyncguard/tests/test_clients.py``
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO / "plugins.v2" / "mediasyncguard"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

LOG_RECORDS = []


def _new_module(name, **attrs):
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


class _StubLogger:
    @staticmethod
    def _append(level, message):
        LOG_RECORDS.append((level, str(message)))

    def info(self, message="", *args, **kwargs):
        self._append("info", message)

    def warn(self, message="", *args, **kwargs):
        self._append("warn", message)

    def warning(self, message="", *args, **kwargs):
        self._append("warning", message)

    def error(self, message="", *args, **kwargs):
        self._append("error", message)

    def debug(self, message="", *args, **kwargs):
        self._append("debug", message)


def _install_base_stubs():
    """安装 clients.py 导入期需要的 MoviePilot 桩（app.log）。"""
    app_pkg = _new_module("app")
    app_pkg.log = _new_module("app.log", logger=_StubLogger())
    app_pkg.types = _new_module("app.types")


_install_base_stubs()

import clients as clients_module  # noqa: E402  (必须在 stub 之后导入)

from config import PluginConfig  # noqa: E402
from models import MediaItem, TorrentItem  # noqa: E402


# ---------------------------------------------------------------------------
# 通用假对象
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.closed = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        self.closed = True


class FakeRequestUtils:
    """可编程的 RequestUtils 桩。"""

    instances = []
    get_handler = None  # staticmethod(url, params) -> FakeResponse|None
    delete_handler = None

    def __init__(self, headers=None, timeout=None):
        self.headers = headers or {}
        self.timeout = timeout
        FakeRequestUtils.instances.append(self)
        self.get_calls = []
        self.delete_calls = []

    def get_res(self, url, params=None, **kwargs):
        self.get_calls.append((url, params))
        if FakeRequestUtils.get_handler is None:
            raise AssertionError("测试未设置 FakeRequestUtils.get_handler")
        return FakeRequestUtils.get_handler(url, params)

    def delete_res(self, url, **kwargs):
        self.delete_calls.append((url, kwargs))
        if FakeRequestUtils.delete_handler is None:
            raise AssertionError("测试未设置 FakeRequestUtils.delete_handler")
        return FakeRequestUtils.delete_handler(url)


def _install_http_stub():
    app_pkg = sys.modules["app"]
    app_pkg.utils = _new_module("app.utils")
    http_module = _new_module("app.utils.http", RequestUtils=FakeRequestUtils)
    app_pkg.utils.http = http_module
    return http_module


def _install_helper_stubs(downloader_helper_cls, mediaserver_helper_cls):
    app_pkg = sys.modules["app"]
    app_pkg.helper = _new_module("app.helper")
    downloader_module = _new_module(
        "app.helper.downloader", DownloaderHelper=downloader_helper_cls
    )
    mediaserver_module = _new_module(
        "app.helper.mediaserver", MediaServerHelper=mediaserver_helper_cls
    )
    app_pkg.helper.downloader = downloader_module
    app_pkg.helper.mediaserver = mediaserver_module
    return downloader_module, mediaserver_module


class FakeQbClient:
    """假 qBittorrent 客户端（MoviePilot Qbittorrent 实例的最小替代）。"""

    def __init__(self, torrents=None, error=False, raise_exc=None, delete_ok=True):
        self.torrents = torrents or []
        self.error = error
        self.raise_exc = raise_exc
        self.delete_ok = delete_ok
        self.get_calls = []
        self.delete_calls = []
        self.reconnects = 0
        self.inactive = False

    def get_torrents(self, ids=None, status=None, tags=None):
        self.get_calls.append({"ids": ids, "status": status, "tags": tags})
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.torrents), self.error

    def delete_torrents(self, delete_file, ids):
        self.delete_calls.append({"delete_file": delete_file, "ids": list(ids)})
        return self.delete_ok

    def is_inactive(self):
        return self.inactive

    def reconnect(self):
        self.reconnects += 1

    def set_torrents(self, torrents):
        self.torrents = torrents


def _normalizer(prefix_from="/downloads", prefix_to="/medias/Downloads"):
    def _normalize(raw_path, downloader):
        text = str(raw_path)
        if text.startswith(prefix_from):
            return prefix_to + text[len(prefix_from):]
        return text

    return _normalize


# ---------------------------------------------------------------------------
# JellyfinApi
# ---------------------------------------------------------------------------
class JellyfinApiTest(unittest.TestCase):
    def setUp(self):
        FakeRequestUtils.instances = []
        FakeRequestUtils.get_handler = None
        FakeRequestUtils.delete_handler = None
        _install_http_stub()
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-clients-jf-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.host = "http://jellyfin:8096"
        self.path_mappings = [("/media", str(self.root))]

    def tearDown(self):
        self._tmp.cleanup()

    def _make_api(self):
        return clients_module.JellyfinApi(
            name="Jellyfin",
            host=self.host,
            apikey="secret-key",
            path_mappings=self.path_mappings,
            timeout=15,
            page_size=2,
        )

    def _write_media(self, relative: str) -> pathlib.Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return target

    def test_constructor_normalizes_host(self):
        api = clients_module.JellyfinApi("JF", "jellyfin:8096/", "key", [])
        self.assertEqual(api.host, "http://jellyfin:8096/")
        self.assertTrue(api.configured)
        api = clients_module.JellyfinApi("JF", "", "key", [])
        self.assertFalse(api.configured)

    def test_convert_directory_movie_expands_video_inodes(self):
        stream = self._write_media("Movies/BDMV Movie/BDMV/STREAM/00000.m2ts")
        stat_result = stream.stat()
        api = self._make_api()
        item, warning = api._convert_item(
            {
                "Id": "movie-1",
                "Type": "Movie",
                "Name": "BDMV Movie",
                "Path": "/media/Movies/BDMV Movie",
            }
        )
        self.assertIsNotNone(item, warning)
        self.assertEqual(warning, "")
        self.assertEqual(item.path, str(self.root / "Movies" / "BDMV Movie"))
        # InodeKey 是 (dev, inode)，转换后必须写回 MediaItem.inode/dev 且不能颠倒
        self.assertEqual(item.inode, stat_result.st_ino)
        self.assertEqual(item.dev, stat_result.st_dev)
        self.assertEqual(item.extra_inode_keys, [])

    def test_test_connection_success(self):
        FakeRequestUtils.get_handler = lambda url, params: FakeResponse(
            200, {"ServerName": "MyJellyfin"}
        )
        ok, message = self._make_api().test()
        self.assertTrue(ok)
        self.assertIn("连接成功", message)
        self.assertIn("MyJellyfin", message)
        utils = FakeRequestUtils.instances[-1]
        self.assertIn("MediaBrowser Token=", utils.headers.get("Authorization", ""))
        self.assertEqual(utils.timeout, 15)

    def test_test_connection_failures(self):
        FakeRequestUtils.get_handler = lambda url, params: None
        ok, message = self._make_api().test()
        self.assertFalse(ok)
        self.assertTrue(message)

        FakeRequestUtils.get_handler = lambda url, params: FakeResponse(401, {})
        ok, message = self._make_api().test()
        self.assertFalse(ok)
        self.assertIn("401", message)

        def _boom(url, params):
            raise RuntimeError("网络错误")

        FakeRequestUtils.get_handler = _boom
        ok, message = self._make_api().test()
        self.assertFalse(ok)
        self.assertIn("网络错误", message)

    def test_list_media_items_pagination_mapping_and_stat(self):
        media_file = self._write_media("Movies/Movie (2020)/Movie (2020).mkv")
        responses = {
            0: FakeResponse(
                200,
                {
                    "TotalRecordCount": 3,
                    "Items": [
                        {
                            "Id": "movie-1",
                            "Name": "Movie (2020)",
                            "Type": "Movie",
                            "Path": "/media/Movies/Movie (2020)/Movie (2020).mkv",
                        },
                        {"Id": "series-1", "Name": "Show", "Type": "Series", "Path": "/media/TV"},
                    ],
                },
            ),
            2: FakeResponse(
                200,
                {
                    "TotalRecordCount": 3,
                    "Items": [
                        {
                            "Id": "ep-1",
                            "Name": "Pilot",
                            "Type": "Episode",
                            "Path": "/media/TV/Show/S01/E01.mkv",
                            "SeriesName": "Show",
                            "SeriesId": "series-9",
                            "ParentId": "season-1",
                            "ParentIndexNumber": 1,
                            "IndexNumber": 2,
                        }
                    ],
                },
            ),
        }

        def _handler(url, params):
            self.assertTrue(url.endswith("/Items"))
            self.assertEqual(params["IncludeItemTypes"], "Movie,Episode")
            return responses[params["StartIndex"]]

        FakeRequestUtils.get_handler = _handler
        items, warnings = self._make_api().list_media_items()
        self.assertEqual(len(items), 2)
        movie = next(item for item in items if item.item_id == "movie-1")
        self.assertEqual(movie.path, str(media_file))
        self.assertNotEqual(movie.inode, 0)
        self.assertEqual(movie.item_type, "Movie")
        self.assertEqual(movie.server, "Jellyfin")
        episode = next(item for item in items if item.item_id == "ep-1")
        self.assertEqual(episode.season, 1)
        self.assertEqual(episode.episode, 2)
        self.assertEqual(episode.series_name, "Show")
        self.assertEqual(episode.series_id, "series-9")
        self.assertEqual(episode.parent_id, "season-1")
        self.assertEqual(episode.inode, 0)
        self.assertTrue(any("不可访问" in warning for warning in warnings))

    def test_list_media_items_failures_raise_client_error(self):
        FakeRequestUtils.get_handler = lambda url, params: None
        with self.assertRaises(clients_module.ClientError):
            self._make_api().list_media_items()

        FakeRequestUtils.get_handler = lambda url, params: FakeResponse(500, {})
        with self.assertRaises(clients_module.ClientError):
            self._make_api().list_media_items()

        FakeRequestUtils.get_handler = lambda url, params: FakeResponse(
            200, ValueError("bad json")
        )
        with self.assertRaises(clients_module.ClientError):
            self._make_api().list_media_items()

    def test_list_media_items_unconfigured(self):
        api = clients_module.JellyfinApi("JF", "", "", [])
        with self.assertRaises(clients_module.ClientError):
            api.list_media_items()

    def test_delete_item_status_codes(self):
        api = self._make_api()

        FakeRequestUtils.delete_handler = lambda url: FakeResponse(204)
        ok, message, status = api.delete_item("abc")
        self.assertTrue(ok)
        self.assertEqual(status, 204)
        self.assertTrue(FakeRequestUtils.instances[-1].delete_calls[0][0].endswith("/Items/abc"))

        FakeRequestUtils.delete_handler = lambda url: FakeResponse(200)
        self.assertEqual(api.delete_item("abc")[0], True)

        FakeRequestUtils.delete_handler = lambda url: FakeResponse(404)
        ok, message, status = api.delete_item("abc")
        self.assertTrue(ok)
        self.assertEqual(status, 404)

        FakeRequestUtils.delete_handler = lambda url: FakeResponse(400, {})
        ok, message, status = api.delete_item("abc")
        self.assertFalse(ok)
        self.assertEqual(status, 400)
        self.assertIn("400", message)

        FakeRequestUtils.delete_handler = lambda url: None
        self.assertFalse(api.delete_item("abc")[0])

        def _raise(url):
            raise RuntimeError("timeout")

        FakeRequestUtils.delete_handler = _raise
        ok, message, status = api.delete_item("abc")
        self.assertFalse(ok)
        self.assertEqual(status, 0)
        self.assertIn("timeout", message)

        self.assertEqual(api.delete_item("")[0], False)


# ---------------------------------------------------------------------------
# QbittorrentApi
# ---------------------------------------------------------------------------
class QbittorrentApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-clients-qb-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.content = self.root / "Downloads" / "Show" / "S01E01.mkv"
        self.content.parent.mkdir(parents=True, exist_ok=True)
        self.content.write_bytes(b"video")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_api(self, client, normalizer=None):
        # 本组测试关注字段转换，默认使用恒等映射（路径映射行为由 build_client_bundle 测试覆盖）
        return clients_module.QbittorrentApi(
            name="主QB",
            client=client,
            normalize_path=normalizer or (lambda raw_path, downloader: str(raw_path)),
        )

    def test_list_torrents_conversion(self):
        raw = {
            "hash": "ABCDEF0123",
            "name": "Show",
            "category": "TV",
            "tags": "MoviePilot自动辅种, 追剧",
            "state": "stalledUP",
            "progress": 1.0,
            "total_size": 12345,
            "size": 100,
            "save_path": str(self.content.parent),
            "content_path": str(self.content.parent),
            "added_on": 100,
            "completion_on": 200,
            "ratio": 1.5,
            "seeding_time": 300,
        }
        client = FakeQbClient(torrents=[raw, {"hash": ""}])
        torrents, warnings = self._make_api(client).list_torrents()
        self.assertEqual(len(torrents), 1)
        item = torrents[0]
        self.assertEqual(item.downloader, "主QB")
        self.assertEqual(item.hash, "abcdef0123")
        self.assertEqual(item.tags, ["MoviePilot自动辅种", "追剧"])
        self.assertEqual(item.size, 12345)
        self.assertEqual(item.save_path, str(self.content.parent))
        self.assertEqual(item.content_path, str(self.content.parent))
        self.assertTrue(item.path_exists)
        self.assertFalse(item.unsupported_path)
        self.assertEqual(warnings, [])

    def test_list_torrents_invisible_path_marked_unsupported(self):
        raw = {
            "hash": "deadbeef",
            "name": "Old",
            "save_path": "/tank/seedings/Old",
            "content_path": "/tank/seedings/Old",
        }
        torrents, _warnings = self._make_api(FakeQbClient(torrents=[raw])).list_torrents()
        self.assertEqual(len(torrents), 1)
        self.assertFalse(torrents[0].path_exists)
        self.assertTrue(torrents[0].unsupported_path)

    def test_list_torrents_falls_back_to_save_path_name(self):
        raw = {
            "hash": "abc",
            "name": "S01E01.mkv",
            "save_path": str(self.content.parent),
        }
        torrents, _warnings = self._make_api(FakeQbClient(torrents=[raw])).list_torrents()
        self.assertEqual(torrents[0].content_path, str(self.content))

    def test_list_torrents_errors(self):
        with self.assertRaises(clients_module.ClientError):
            self._make_api(FakeQbClient(error=True)).list_torrents()
        with self.assertRaises(clients_module.ClientError):
            self._make_api(FakeQbClient(raise_exc=RuntimeError("conn refused"))).list_torrents()
        with self.assertRaises(clients_module.ClientError):
            clients_module.QbittorrentApi("QB", None, _normalizer()).list_torrents()

    def test_normalize_path_failure_falls_back(self):
        def _boom(raw_path, downloader):
            raise RuntimeError("mapping failed")

        raw = {
            "hash": "abc",
            "save_path": "/downloads/Show",
            "content_path": "/downloads/Show",
        }
        api = clients_module.QbittorrentApi("QB", FakeQbClient(torrents=[raw]), _boom)
        torrents, _ = api.list_torrents()
        self.assertEqual(torrents[0].content_path, "/downloads/Show")

    def test_test_connection(self):
        client = FakeQbClient(torrents=[{"hash": "a"}])
        ok, message = self._make_api(client).test()
        self.assertTrue(ok)
        self.assertIn("连接成功", message)

        ok, message = self._make_api(FakeQbClient(error=True)).test()
        self.assertFalse(ok)
        self.assertIn("失败", message)

        junk = SimpleNamespace(is_inactive=lambda: True, reconnect=lambda: None,
                               get_torrents=lambda: ([], False))
        ok, _ = clients_module.QbittorrentApi("QB", junk, _normalizer()).test()
        self.assertTrue(ok)
        self.assertFalse(self._make_api(None).test()[0])

    def test_delete_torrents(self):
        client = FakeQbClient()
        api = self._make_api(client)
        ok, message = api.delete_torrents(["ABC", "def"], True)
        self.assertTrue(ok)
        self.assertEqual(client.delete_calls[0]["ids"], ["abc", "def"])
        self.assertTrue(client.delete_calls[0]["delete_file"])

        ok, _ = api.delete_torrents([], True)
        self.assertFalse(ok)

        ok, _ = self._make_api(FakeQbClient(delete_ok=False)).delete_torrents(["abc"], False)
        self.assertFalse(ok)

        ok, _ = clients_module.QbittorrentApi("QB", None, _normalizer()).delete_torrents(["a"], True)
        self.assertFalse(ok)

        class _NoDelete:
            def get_torrents(self, ids=None):
                return [], False

        ok, message = clients_module.QbittorrentApi("QB", _NoDelete(), _normalizer()).delete_torrents(
            ["abc"], True
        )
        self.assertFalse(ok)
        self.assertIn("不支持", message)

    def test_get_existing_hashes(self):
        client = FakeQbClient(torrents=[{"hash": "AAA"}, {"hash": "bbb"}])
        existing = self._make_api(client).get_existing_hashes(["aaa", "ccc"])
        self.assertEqual(existing, {"aaa", "bbb"})
        self.assertEqual(client.get_calls[-1]["ids"], ["aaa", "ccc"])

        self.assertEqual(self._make_api(FakeQbClient()).get_existing_hashes([]), set())
        with self.assertRaises(clients_module.ClientError):
            self._make_api(FakeQbClient(error=True)).get_existing_hashes(["a"])
        with self.assertRaises(clients_module.ClientError):
            self._make_api(FakeQbClient(raise_exc=RuntimeError("x"))).get_existing_hashes(["a"])


# ---------------------------------------------------------------------------
# ServiceCatalog / build_client_bundle
# ---------------------------------------------------------------------------
def _qb_conf(name="主QB", default=True, enabled=True, path_mapping=None):
    return SimpleNamespace(
        name=name,
        type="qbittorrent",
        default=default,
        enabled=enabled,
        config={"host": "qb", "port": 8085},
        path_mapping=path_mapping or [("/downloads", "/medias/Downloads")],
    )


def _jf_conf(name="Jellyfin", enabled=True, host="http://jellyfin:8096", apikey="key"):
    return SimpleNamespace(
        name=name,
        type="jellyfin",
        enabled=enabled,
        config={"host": host, "apikey": apikey},
    )


class _FakeDownloaderHelper:
    configs = {}
    services = {}

    def get_configs(self, include_disabled=False):
        return dict(self.configs)

    def get_service(self, name, type_filter=None):
        return self.services.get(name)


class _FakeMediaServerHelper:
    configs = {}
    services = {}

    def get_configs(self, include_disabled=False):
        return dict(self.configs)

    def get_service(self, name, type_filter=None):
        return self.services.get(name)


class ServiceCatalogTest(unittest.TestCase):
    def setUp(self):
        _install_helper_stubs(self._make_downloader_helper, self._make_mediaserver_helper)
        self.config = PluginConfig()

    @staticmethod
    def _make_downloader_helper():
        return _FakeDownloaderHelper()

    @staticmethod
    def _make_mediaserver_helper():
        return _FakeMediaServerHelper()

    def test_list_downloaders_filters_type_and_enabled(self):
        _FakeDownloaderHelper.configs = {
            "主QB": _qb_conf("主QB"),
            "副QB": _qb_conf("副QB", default=False),
            "TR": SimpleNamespace(name="TR", type="transmission", enabled=True),
            "off": _qb_conf("off", enabled=False),
        }
        options = clients_module.ServiceCatalog().list_downloaders()
        self.assertEqual([item.name for item in options], ["主QB", "副QB"])
        self.assertTrue(options[0].default)
        self.assertEqual(options[0].path_mappings, [("/downloads", "/medias/Downloads")])

    def test_list_media_servers_filters_type_and_reads_host(self):
        _FakeMediaServerHelper.configs = {
            "Jellyfin": _jf_conf(),
            "Emby": SimpleNamespace(name="Emby", type="emby", enabled=True, config={}),
        }
        options = clients_module.ServiceCatalog().list_media_servers()
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].name, "Jellyfin")
        self.assertEqual(options[0].host, "http://jellyfin:8096")

    def test_catalog_swallows_helper_errors(self):
        class _Boom:
            def __init__(self):
                raise RuntimeError("no config")

        _install_helper_stubs(_Boom, _Boom)
        self.assertEqual(clients_module.ServiceCatalog().list_downloaders(), [])
        self.assertEqual(clients_module.ServiceCatalog().list_media_servers(), [])


class BuildClientBundleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-clients-bundle-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.content = self.root / "Downloads" / "a.mkv"
        self.content.parent.mkdir(parents=True, exist_ok=True)
        self.content.write_bytes(b"video")
        self.qb_client = FakeQbClient(
            torrents=[{"hash": "abc", "save_path": str(self.content.parent),
                       "content_path": str(self.content)}]
        )
        self.qb_module = SimpleNamespace(normalize_return_path=self._normalize_return_path)
        self.qb_service = SimpleNamespace(
            name="主QB", instance=self.qb_client, module=self.qb_module, type="qbittorrent"
        )
        self.jf_service = SimpleNamespace(
            name="Jellyfin",
            instance=object(),
            module=object(),
            type="jellyfin",
            config=_jf_conf(),
        )
        _FakeDownloaderHelper.configs = {"主QB": _qb_conf()}
        _FakeDownloaderHelper.services = {"主QB": self.qb_service}
        _FakeMediaServerHelper.configs = {"Jellyfin": _jf_conf()}
        _FakeMediaServerHelper.services = {"Jellyfin": self.jf_service}
        _install_helper_stubs(self._make_downloader_helper, self._make_mediaserver_helper)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _make_downloader_helper():
        return _FakeDownloaderHelper()

    @staticmethod
    def _make_mediaserver_helper():
        return _FakeMediaServerHelper()

    @staticmethod
    def _normalize_return_path(path, downloader):
        # 模拟 MoviePilot 模块签名：接受 Path 对象
        return str(path).replace("/downloads", "/medias/Downloads")

    def test_build_with_explicit_selection(self):
        config = PluginConfig(downloaders=["主QB"], media_servers=["Jellyfin"])
        bundle = clients_module.build_client_bundle(config)
        self.assertEqual([api.name for api in bundle.qbittorrent], ["主QB"])
        self.assertEqual([api.name for api in bundle.jellyfin], ["Jellyfin"])
        self.assertEqual(bundle.warnings, [])
        # 打包时传入的 normalize_path 应使用 module.normalize_return_path
        normalized = bundle.qbittorrent[0].normalize_path("/downloads/x", "主QB")
        self.assertEqual(normalized, "/medias/Downloads/x")
        jellyfin = bundle.jellyfin[0]
        self.assertEqual(jellyfin.host, "http://jellyfin:8096/")
        self.assertEqual(jellyfin.path_mappings, config.media_path_mappings)

    def test_build_defaults_when_config_lists_empty(self):
        config = PluginConfig(downloaders=[], media_servers=[])
        bundle = clients_module.build_client_bundle(config)
        self.assertEqual([api.name for api in bundle.qbittorrent], ["主QB"])
        self.assertEqual([api.name for api in bundle.jellyfin], ["Jellyfin"])

    def test_build_missing_and_unsupported_services(self):
        config = PluginConfig(downloaders=["不存在"], media_servers=["不存在"])
        with self.assertRaises(clients_module.ClientError):
            clients_module.build_client_bundle(config)

        _FakeDownloaderHelper.services = {}
        config = PluginConfig(downloaders=["主QB"], media_servers=["Jellyfin"])
        bundle = clients_module.build_client_bundle(config)
        self.assertEqual(bundle.qbittorrent, [])
        self.assertEqual(len(bundle.jellyfin), 1)
        self.assertTrue(any("主QB" in warning for warning in bundle.warnings))

    def test_build_unsupported_downloader_type(self):
        _FakeDownloaderHelper.services = {
            "主QB": SimpleNamespace(
                name="主QB", instance=FakeQbClient(), module=None, type="transmission"
            )
        }
        config = PluginConfig(downloaders=["主QB"], media_servers=["Jellyfin"])
        bundle = clients_module.build_client_bundle(config)
        self.assertEqual(bundle.qbittorrent, [])
        self.assertTrue(any("暂不支持" in warning for warning in bundle.warnings))

    def test_build_jellyfin_without_host_or_apikey(self):
        _FakeMediaServerHelper.services = {
            "Jellyfin": SimpleNamespace(
                name="Jellyfin",
                instance=object(),
                module=object(),
                type="jellyfin",
                config=SimpleNamespace(config={"host": "", "apikey": ""}),
            )
        }
        config = PluginConfig(downloaders=["主QB"], media_servers=["Jellyfin"])
        bundle = clients_module.build_client_bundle(config)
        self.assertEqual(bundle.jellyfin, [])
        self.assertTrue(any("host/apikey" in warning for warning in bundle.warnings))

    def test_build_helper_failure_is_reported_not_raised(self):
        class _Boom:
            def __init__(self):
                raise RuntimeError("service unavailable")

        _install_helper_stubs(_Boom, _Boom)
        with self.assertRaises(clients_module.ClientError):
            clients_module.build_client_bundle(PluginConfig())


if __name__ == "__main__":
    unittest.main(verbosity=2)
