"""MediaSyncGuard 插件装载与接口测试（MoviePilot 环境用桩替代）。

运行：``python3 plugins.v2/mediasyncguard/tests/test_plugin.py``
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
REPO = PLUGIN_DIR.parents[1]

sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

import stubs  # noqa: E402

stubs.install_stubs()

import mediasyncguard as plugin_module  # noqa: E402
from mediasyncguard import MediaSyncGuard  # noqa: E402


def _iter_form_props(node):
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict) and "model" in props:
            yield props
        for value in node.values():
            yield from _iter_form_props(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_form_props(item)


class PluginMetadataTest(unittest.TestCase):
    def tearDown(self):
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            plugin.stop_service()

    def test_metadata(self):
        self.assertEqual(MediaSyncGuard.plugin_name, "媒体同步守护")
        self.assertTrue(MediaSyncGuard.plugin_version)
        self.assertEqual(MediaSyncGuard.plugin_config_prefix, "mediasyncguard_")
        self.assertTrue(MediaSyncGuard.plugin_icon.endswith(".png"))
        self.assertEqual(MediaSyncGuard.plugin_author, "TimoYoung")
        self.assertEqual(MediaSyncGuard.auth_level, 1)
        self.assertTrue(MediaSyncGuard.__doc__)

    def test_render_mode_and_dist_artifacts(self):
        mode, dist = MediaSyncGuard.get_render_mode()
        self.assertEqual(mode, "vue")
        self.assertEqual(dist, "dist/assets")
        remote_entry = PLUGIN_DIR / dist / "remoteEntry.js"
        self.assertTrue(remote_entry.is_file(), f"缺少前端构建产物：{remote_entry}")
        index_html = PLUGIN_DIR / "dist" / "index.html"
        self.assertTrue(index_html.is_file())
        # federation 暴露的组件名应包含 Config / Page 文案
        text = remote_entry.read_text(encoding="utf-8", errors="replace")
        self.assertIn("MediaSyncGuard", text)

    def test_market_entry(self):
        package = json.loads((REPO / "package.v2.json").read_text(encoding="utf-8"))
        entry = package.get("MediaSyncGuard")
        self.assertIsNotNone(entry, "package.v2.json 缺少 MediaSyncGuard 条目")
        self.assertEqual(entry["version"], MediaSyncGuard.plugin_version)
        self.assertEqual(entry["name"], MediaSyncGuard.plugin_name)
        self.assertEqual(entry["icon"], MediaSyncGuard.plugin_icon)
        self.assertEqual(entry["level"], MediaSyncGuard.auth_level)
        self.assertTrue(entry["v2"])
        self.assertTrue((REPO / "icons" / MediaSyncGuard.plugin_icon).is_file())
        self.assertIn(f"v{MediaSyncGuard.plugin_version}", entry["history"])

    def test_form_keys_and_defaults(self):
        self.plugin = MediaSyncGuard()
        self.plugin.init_plugin({})
        form, model = self.plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertTrue(form)
        models = {props["model"] for props in _iter_form_props(form)}
        for key in (
            "enabled",
            "notify",
            "dry_run",
            "downloaders",
            "media_servers",
            "media_path_map",
            "poll_seconds",
            "grace_seconds",
            "batch_limit_media",
            "batch_limit_torrents",
            "ignore_categories",
            "ignore_tags",
            "auto_media_to_torrent",
            "auto_torrent_to_media",
            "auto_delete_files",
            "delete_torrent_files",
            "delete_media_sidecars",
            "cleanup_empty_dirs",
            "clean_moviepilot_history",
            "scan_include_unfinished",
            "media_delete_roots",
            "download_delete_roots",
        ):
            self.assertIn(key, models, f"配置表单缺少 {key}")
        # 默认安全值
        self.assertFalse(model["enabled"])
        self.assertTrue(model["dry_run"])
        self.assertFalse(model["auto_media_to_torrent"])
        self.assertFalse(model["auto_torrent_to_media"])
        self.assertEqual(model["poll_seconds"], 60)

    def test_api_routes(self):
        self.plugin = MediaSyncGuard()
        self.plugin.init_plugin({})
        apis = self.plugin.get_api()
        paths = {api["path"] for api in apis}
        self.assertTrue(
            {
                "/status",
                "/options",
                "/scan",
                "/scan/result",
                "/scan/cancel",
                "/apply",
                "/pending",
                "/pending/cancel",
                "/poll",
                "/test",
            }.issubset(paths)
        )
        for api in apis:
            self.assertIn(api["methods"][0], {"GET", "POST"})
            self.assertTrue(callable(api["endpoint"]))
            self.assertEqual(api.get("auth"), "bear")

    def test_service_registration(self):
        self.plugin = MediaSyncGuard()
        self.plugin.init_plugin({"enabled": False})
        self.assertEqual(self.plugin.get_state(), False)
        self.assertEqual(self.plugin.get_service(), [])

        self.plugin.init_plugin({"enabled": True, "poll_seconds": 90})
        self.assertTrue(self.plugin.get_state())
        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        service = services[0]
        self.assertEqual(service["trigger"], "interval")
        self.assertEqual(service["kwargs"]["seconds"], 90)

    def test_api_endpoints_are_defensive(self):
        self.plugin = MediaSyncGuard()
        self.plugin.init_plugin({"enabled": True, "notify": False})
        status = self.plugin.api_status()
        self.assertIsInstance(status, dict)
        self.assertIn("configured", status)
        options = self.plugin.api_options()
        self.assertIsInstance(options, dict)
        self.assertIn("downloaders", options)
        self.assertIn("media_servers", options)
        scan_state = self.plugin.api_scan_result()
        self.assertIsInstance(scan_state, dict)
        self.assertIn("result", scan_state)
        pending = self.plugin.api_pending()
        self.assertIsInstance(pending, dict)
        self.assertIn("pending", pending)

    def test_plugin_module_does_not_import_heavy_optional_deps(self):
        # 插件只能依赖 MoviePilot 自带依赖；不需要 whisper/psutil 等第三方库。
        self.assertFalse(hasattr(plugin_module, "MediaSyncGuard") is False)
        source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        for forbidden in ("faster_whisper", "psutil", "import srt", "lxml"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
