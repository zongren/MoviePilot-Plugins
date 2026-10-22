"""MediaSyncGuard 安全门禁回归测试（独立安全审计发现的问题）。

覆盖：
- H1：选中服务未成功读取/未构建时，复核不能变成空判断（本轮 partial，禁止执行）；
- H2：下载器路径不可见/存在未纳管下载器时，禁止自动删文件与自动删媒体；
- M3：Jellyfin 分页提前返回短页但 TotalRecordCount 表明还有数据 → 必须报错；
- M4：插件停用/停止中时，手动轮询不得执行待办删除；
- M5：删除媒体的复核必须同时通过下载目录/根目录安全阀。

运行：``python3 plugins.v2/mediasyncguard/tests/test_safety_gates.py``
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import time
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent

sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

import test_runtime as T  # noqa: E402  （安装 MoviePilot/核心模块桩，并提供测试基类）

from config import PluginConfig  # noqa: E402
from models import MediaItem, PendingAction, Snapshot, TorrentItem  # noqa: E402


def _load_real_module(name: str, filename: str):
    """绕过 test_runtime 安装的 guard 桩，加载真实的 guard/cleanup 模块。"""
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REAL_GUARD = _load_real_module("mediasyncguard_real_guard", "guard.py")
REAL_LINKER = _load_real_module("mediasyncguard_real_linker", "linker.py")


class CoverageGateTest(T.RuntimeTestBase):
    """H1：数据源覆盖不完整时禁止生成与执行真实删除。"""

    def _pending_due_action(self):
        action = self._action()
        action.due_at = time.time() - 1
        self.runtime._pending = [action]
        return action

    def test_missing_expected_media_server_marks_partial_and_holds_actions(self):
        self.jellyfin.items = [self._media("m1")]
        self.qb.torrents = [self._torrent("abc")]
        self.runtime._expected_media_servers = {"Jellyfin", "MissingJellyfin"}
        action = self._pending_due_action()
        summary = self.runtime.poll_once()
        self.assertTrue(summary["partial"], summary)
        self.assertTrue(any("不可用" in err for err in summary["errors"]), summary)
        self.assertEqual(summary["actions_executed"], 0)
        self.assertIn(action, self.runtime._pending)
        self.assertEqual(self.qb.delete_calls, [])

    def test_missing_expected_downloader_holds_media_actions(self):
        self.qb.torrents = [self._torrent("keep")]
        self.runtime._expected_downloaders = {"主QB", "副QB"}
        action = PendingAction(
            action_id="media-1",
            kind="delete_media",
            created_at=time.time(),
            due_at=time.time() - 1,
            reason="测试",
            payload={"items": [{"server": "Jellyfin", "item_id": "m1"}]},
        )
        self.runtime._pending = [action]
        summary = self.runtime.poll_once()
        self.assertTrue(summary["partial"], summary)
        self.assertEqual(summary["actions_executed"], 0)
        self.assertIn(action, self.runtime._pending)


class UnverifiableDownloaderGateTest(T.RuntimeTestBase):
    """H2：下载器路径不可见/未纳管时禁止自动删媒体与删文件。"""

    def test_invisible_downloader_disables_media_delete_but_not_visible_file_checks(self):
        invisible = TorrentItem(
            downloader="主QB",
            hash="abc",
            name="x",
            progress=1.0,
            path_exists=False,
            unsupported_path=True,
        )
        self.qb.torrents = [invisible]
        summary = self.runtime.poll_once()
        self.assertFalse(self.runtime._downloaders_verifiable)
        # 删文件只依赖可见种子/媒体的 inode 共享检查；数据源覆盖完整时允许删除
        self.assertTrue(self.runtime._file_deletion_safe)
        self.assertFalse(summary["partial"], summary)
        status = self.runtime.get_status()
        self.assertFalse(status["coverage"]["downloaders_verifiable"])
        self.assertTrue(status["coverage"]["file_deletion_safe"])
        self.assertTrue(any("已禁用自动删媒体" in w for w in status["warnings"]), status["warnings"])

    def test_unselected_enabled_downloader_also_blocks_file_and_media_deletion(self):
        clients_module = T.clients_module
        original_resolve = T.runtime_module.resolve_service_selection
        T.runtime_module.resolve_service_selection = lambda config: clients_module.ServiceSelection(
            downloaders=["主QB"],
            media_servers=["Jellyfin"],
            enabled_downloaders=["主QB", "副QB"],
            enabled_media_servers=["Jellyfin"],
            configured_downloaders=["主QB", "副QB"],
            configured_media_servers=["Jellyfin"],
        )
        try:
            self.qb.torrents = [self._torrent("abc")]
            runtime = self._new_runtime(self.config)
            action = PendingAction(
                action_id="media-1",
                kind="delete_media",
                created_at=time.time(),
                due_at=time.time() - 1,
                reason="测试",
                payload={"items": [{"server": "Jellyfin", "item_id": "m1"}]},
            )
            runtime._pending = [action]
            runtime.poll_once()
            self.assertFalse(runtime._downloaders_verifiable)
            self.assertIn(action, runtime._pending)
            self.assertEqual(self.jellyfin.deleted, [])
        finally:
            T.runtime_module.resolve_service_selection = original_resolve

    def test_direction_b_planning_uses_effective_config_with_auto_off(self):
        self.jellyfin.items = [self._media("m1")]
        self.qb.torrents = [
            TorrentItem(
                downloader="主QB",
                hash="abc",
                name="x",
                progress=1.0,
                path_exists=False,
                unsupported_path=True,
            )
        ]
        self.runtime._config = self.runtime._config.merged(
            auto_torrent_to_media=True,
            auto_media_to_torrent=False,
        )
        T.PLAN_CALLS.clear()
        self.runtime.poll_once()
        self.assertTrue(T.PLAN_CALLS, "本轮应仍执行 plan_cycle 以提供候选展示")
        planned_config = T.PLAN_CALLS[-1]["args"][0]
        self.assertFalse(planned_config.auto_torrent_to_media)
        # 删文件不再被“下载器不可校验”全局禁止，仍由每个种子的可见共享检查决定
        self.assertTrue(planned_config.auto_delete_files)


class DisabledPollGateTest(T.RuntimeTestBase):
    """M4：停用/停止中不允许执行待办真实删除。"""

    def test_disabled_poll_does_not_execute_pending_actions(self):
        self.qb.torrents = [self._torrent("abc")]
        runtime = self._new_runtime(
            self.config.merged(enabled=False, dry_run=False, auto_media_to_torrent=True)
        )
        action = self._action()
        action.due_at = time.time() - 1
        runtime._pending = [action]
        summary = runtime.trigger_poll()
        self.assertEqual(summary["actions_executed"], 0)
        self.assertIn(action, runtime._pending)
        self.assertEqual(self.qb.delete_calls, [])

    def test_stopping_flag_prevents_execution(self):
        self.qb.torrents = [self._torrent("abc")]
        runtime = self._new_runtime(
            self.config.merged(enabled=True, dry_run=False, auto_media_to_torrent=True)
        )
        runtime._stopping = True
        action = self._action()
        action.due_at = time.time() - 1
        runtime._pending = [action]
        self.assertEqual(runtime._execute_due_actions(time.time()), 0)
        self.assertEqual(self.qb.delete_calls, [])


class EmptyCoverageGateTest(T.RuntimeTestBase):
    """M-A：配置未显式选择服务/媒体数据源为空时，禁止把“无数据”当成完整覆盖。"""

    def test_no_media_source_marks_partial_and_holds_actions(self):
        self.qb.torrents = [self._torrent("abc")]
        # 模拟：插件配置 media_servers=[]（默认全部）但媒体服务在配置阶段不可用，
        # 客户端集合里没有任何 Jellyfin；期望媒体集合为空。
        self.runtime._clients = T.SimpleNamespace(
            jellyfin=[], qbittorrent=[self.qb], warnings=[]
        )
        self.runtime._expected_media_servers = set()
        self.runtime._expected_downloaders = {"主QB"}
        action = self._action()
        action.due_at = time.time() - 1
        self.runtime._pending = [action]
        summary = self.runtime.poll_once()
        self.assertTrue(summary["partial"], summary)
        self.assertTrue(any("无可用 Jellyfin" in err for err in summary["errors"]), summary)
        self.assertEqual(summary["actions_executed"], 0)
        self.assertIn(action, self.runtime._pending)
        self.assertEqual(self.qb.delete_calls, [])


class UnmonitoredServiceGateTest(T.RuntimeTestBase):
    """M-D/M-E：未纳管（含临时禁用）的下载器/媒体服务器也要降级保护。"""

    def _patch_selection(self, selection):
        self._original_resolve = T.runtime_module.resolve_service_selection
        T.runtime_module.resolve_service_selection = lambda config: selection

    def tearDown(self):
        original = getattr(self, "_original_resolve", None)
        if original is not None:
            T.runtime_module.resolve_service_selection = original
        super().tearDown()

    def test_configured_but_disabled_downloader_disables_auto_media_delete(self):
        clients_module = T.clients_module
        self._patch_selection(
            clients_module.ServiceSelection(
                downloaders=["主QB"],
                media_servers=["Jellyfin"],
                enabled_downloaders=["主QB"],
                enabled_media_servers=["Jellyfin"],
                configured_downloaders=["主QB", "副QB"],
                configured_media_servers=["Jellyfin"],
            )
        )
        self.qb.torrents = [self._torrent("abc")]
        runtime = self._new_runtime(self.config)
        runtime.poll_once()
        self.assertFalse(runtime._downloaders_verifiable)
        self.assertTrue(runtime._file_deletion_safe)
        self.assertTrue(any("未纳入监控的下载器" in w for w in runtime.get_status()["warnings"]))

    def test_unmonitored_second_jellyfin_blocks_torrent_deletion(self):
        clients_module = T.clients_module
        self._patch_selection(
            clients_module.ServiceSelection(
                downloaders=["主QB"],
                media_servers=["Jellyfin"],
                enabled_downloaders=["主QB"],
                enabled_media_servers=["Jellyfin", "Jellyfin2"],
                configured_downloaders=["主QB"],
                configured_media_servers=["Jellyfin", "Jellyfin2"],
            )
        )
        self.qb.torrents = [self._torrent("abc")]
        action = self._action()
        action.due_at = time.time() - 1
        runtime = self._new_runtime(self.config.merged(auto_media_to_torrent=True, dry_run=False))
        runtime._pending = [action]
        result = runtime.poll_once()
        self.assertFalse(runtime._media_to_torrent_verifiable)
        self.assertFalse(result.get("actions_executed", 0))
        self.assertEqual(self.qb.delete_calls, [])
        self.assertTrue(
            any("未纳入监控的媒体服务器（含已禁用" in w for w in runtime.get_status()["warnings"])
        )


class ManualApplySafetyTest(T.ScanAndApplyTest):
    """M-B/M-C 与新增覆盖门禁：手工删除也要过实时数据/可校验性检查。"""

    def _movie_row(self):
        movie = self.media_root / "Movie" / "movie.mkv"
        movie.parent.mkdir(parents=True, exist_ok=True)
        movie.write_bytes(b"video")
        row = self._media_row(movie)
        self._run_scan(self._result(media_rows=[row]))
        self._seed_live_state(media_rows=[row])
        return row

    def test_manual_media_apply_allowed_when_all_hardlinks_are_visible(self):
        """下载器不可校验，但 nlink 能被可见引用完全解释时允许手动删媒体（转码衍生内容）。"""
        row = self._movie_row()
        self.runtime._downloaders_verifiable = False
        response = self.runtime.apply_scan_selection({"kind": "media", "ids": [row["id"]]})
        self.assertTrue(response["success"], response)
        self.assertIn(row["item_id"], self.jellyfin.deleted)

    def test_manual_media_apply_blocked_when_unknown_hardlink_exists(self):
        row = self._movie_row()
        extra_link = self.media_root / "hidden-extra-link.mkv"
        os.link(row["path"], extra_link)
        try:
            response = self.runtime.apply_scan_selection({"kind": "media", "ids": [row["id"]]})
        finally:
            os.unlink(extra_link)
        self.assertFalse(response["success"], response)
        self.assertTrue(
            any("无法校验的硬链接" in item["error"] for item in response["failed"]),
            response["failed"],
        )
        self.assertEqual(self.jellyfin.deleted, [])

    def test_manual_media_apply_blocked_when_live_inode_missing(self):
        row = self._movie_row()
        # 文件在扫描后不可读：Jellyfin 仍有条目但实时 inode=0
        live = self.runtime._snapshot.media[row["id"]]
        broken = MediaItem.from_dict(live.to_dict())
        broken.inode = 0
        broken.dev = 0
        self.runtime._snapshot.media[row["id"]] = broken
        response = self.runtime.apply_scan_selection({"kind": "media", "ids": [row["id"]]})
        self.assertFalse(response["success"], response)
        self.assertTrue(any("无法校验媒体文件 inode" in item["error"] for item in response["failed"]))
        self.assertEqual(self.jellyfin.deleted, [])

    def test_manual_torrent_apply_deletes_files_when_visible_checks_pass(self):
        rows = [self._torrent_row("abc")]
        self._run_scan(self._result(torrent_rows=rows))
        self._seed_live_state(torrent_rows=rows)
        self.runtime._downloaders_verifiable = False
        response = self.runtime.apply_scan_selection(
            {"kind": "torrents", "ids": ["主QB|abc"], "delete_files": True}
        )
        self.assertTrue(response["success"], response)
        self.assertEqual(self.qb.delete_calls[0]["delete_files"], True)
        self.assertTrue(any("未纳入监控的下载器" in warning for warning in response["warnings"]))

    def test_manual_apply_rejected_when_coverage_incomplete(self):
        row = self._movie_row()
        self.runtime._coverage_complete = False
        response = self.runtime.apply_scan_selection({"kind": "media", "ids": [row["id"]]})
        self.assertFalse(response["success"], response)
        self.assertIn("实时数据不完整", response["message"])
        self.assertEqual(self.jellyfin.deleted, [])


class ScanIgnoreKeywordTest(unittest.TestCase):
    """一次性扫描忽略关键词：转码/重新压制后无硬链接的内容可按业务关系隐藏。"""

    def _snapshot_and_links(self, media_path, torrent_name, torrent_path):
        media = MediaItem(
            server="Jellyfin",
            item_id="m1",
            path=media_path,
            inode=83256,
            dev=58,
            item_type="Episode",
            series_name="老友记",
            season=0,
            episode=1,
        )
        torrent = TorrentItem(
            downloader="主QB",
            hash="abc",
            name=torrent_name,
            save_path="/medias/Downloads/Others",
            content_path=torrent_path,
            progress=1.0,
            size=1,
        )
        snapshot = Snapshot(media={media.key: media}, torrents={torrent.key: torrent})
        links = REAL_LINKER.LinkIndex()
        # 让种子有视频 inode 但没有任何媒体 inode 关联（模拟“无关联”状态）
        links.add_torrent_inode((58, 99999), torrent)
        return snapshot, links

    def test_keywords_hide_media_and_torrent_rows(self):
        snapshot, links = self._snapshot_and_links(
            "/medias/TV/老友记 (1994)/Specials/老友记 - S00E001.mkv",
            "Friends Bonus Disc (2024)",
            "/medias/Downloads/Others/Friends Bonus Disc (2024)",
        )
        config = PluginConfig(
            scan_ignore_keywords=["Friends Bonus Disc", "老友记 (1994)/Specials"]
        )
        result = REAL_GUARD.build_scan(config, snapshot, links)
        self.assertEqual(result["media_without_torrent"], [])
        self.assertEqual(result["torrents_without_media"], [])
        self.assertEqual(result["skipped"]["ignored_keyword"], 2)

    def test_without_keywords_rows_are_reported(self):
        snapshot, links = self._snapshot_and_links(
            "/medias/TV/老友记 (1994)/Specials/老友记 - S00E001.mkv",
            "Friends Bonus Disc (2024)",
            "/medias/Downloads/Others/Friends Bonus Disc (2024)",
        )
        result = REAL_GUARD.build_scan(PluginConfig(), snapshot, links)
        self.assertEqual(len(result["media_without_torrent"]), 1)
        self.assertEqual(len(result["torrents_without_media"]), 1)
        self.assertEqual(result["skipped"]["ignored_keyword"], 0)


class JellyfinPaginationGateTest(unittest.TestCase):
    """M3：分页短页但 TotalRecordCount 未取完 → 必须抛 ClientError。"""

    def test_short_page_with_remaining_total_raises(self):
        clients_module = T.clients_module
        api = clients_module.JellyfinApi(
            name="Jellyfin",
            host="http://127.0.0.1:8096",
            apikey="k",
            path_mappings=[],
            page_size=5000,
        )
        rows = [
            {"Id": f"id{i}", "Type": "Movie", "Name": f"m{i}", "Path": f"/media/m{i}.mkv"}
            for i in range(3)
        ]

        def fake_get_json(path, params=None):
            return {"Items": rows, "TotalRecordCount": 100}

        api._get_json = fake_get_json  # type: ignore[method-assign]
        with self.assertRaises(clients_module.ClientError):
            api.list_media_items()

    def test_complete_short_page_is_ok(self):
        clients_module = T.clients_module
        api = clients_module.JellyfinApi(
            name="Jellyfin",
            host="http://127.0.0.1:8096",
            apikey="k",
            path_mappings=[],
            page_size=5000,
        )
        rows = [
            {"Id": f"id{i}", "Type": "Movie", "Name": f"m{i}", "Path": f"/media/m{i}.mkv"}
            for i in range(3)
        ]

        def fake_get_json(path, params=None):
            return {"Items": rows, "TotalRecordCount": 3}

        api._get_json = fake_get_json  # type: ignore[method-assign]
        items, _warnings = api.list_media_items()
        self.assertEqual(len(items), 3)


class RecheckSafetyValveTest(unittest.TestCase):
    """M5：删除媒体复核必须拒绝下载目录内路径。"""

    def _config(self):
        return PluginConfig(
            media_delete_roots=["/medias"],
            download_delete_roots=["/medias/Downloads"],
        )

    def _action(self, path):
        return PendingAction(
            action_id="a1",
            kind="delete_media",
            created_at=time.time(),
            due_at=time.time(),
            reason="测试",
            payload={
                "items": [
                    {
                        "server": "Jellyfin",
                        "item_id": "m1",
                        "path": path,
                        "inode": 1,
                        "dev": 1,
                    }
                ]
            },
        )

    def test_rejects_download_root_path(self):
        path = "/medias/Downloads/Movie/movie.mkv"
        media = MediaItem(server="Jellyfin", item_id="m1", path=path, inode=1, dev=1)
        current = Snapshot(media={media.key: media})
        ok, reason = REAL_GUARD.recheck_media_action(
            self._action(path), current, REAL_LINKER.LinkIndex(), self._config()
        )
        self.assertFalse(ok)
        self.assertIn("安全阀", reason)

    def test_accepts_normal_media_path(self):
        path = "/medias/Movies/Movie (2020)/movie.mkv"
        media = MediaItem(server="Jellyfin", item_id="m1", path=path, inode=1, dev=1)
        current = Snapshot(media={media.key: media})
        ok, reason = REAL_GUARD.recheck_media_action(
            self._action(path), current, REAL_LINKER.LinkIndex(), self._config()
        )
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
