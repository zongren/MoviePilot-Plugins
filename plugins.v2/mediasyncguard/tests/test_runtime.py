"""MediaSyncGuard 运行时（runtime.py）单元测试。

使用假客户端 / 假核心模块（guard、linker），真实 state 模块与临时目录；
不发起任何网络请求，也不连接真实 Jellyfin/qBittorrent/MoviePilot。

运行：``python3 plugins.v2/mediasyncguard/tests/test_runtime.py``
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import time
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


# ---------------------------------------------------------------------------
# MoviePilot 桩
# ---------------------------------------------------------------------------
app_pkg = _new_module("app")
app_pkg.log = _new_module("app.log", logger=_StubLogger())
app_pkg.schemas = _new_module("app.schemas")
app_pkg.schemas.types = _new_module(
    "app.schemas.types", NotificationType=SimpleNamespace(Plugin="Plugin")
)


# ---------------------------------------------------------------------------
# 核心模块桩（guard / linker）。state 使用真实实现（临时目录持久化）。
# ---------------------------------------------------------------------------
GUARD_HOOKS = {}
PLAN_CALLS = []
LINK_CALLS = []
SCAN_HOOK_RESULT = {}


def _default_plan_cycle(*args, **kwargs):
    PLAN_CALLS.append({"args": args, "kwargs": kwargs})
    return SimpleNamespace(
        torrent_candidates=[], media_candidates=[], actions=[], notices=[], summary={}
    )


def _default_build_scan(*args, **kwargs):
    return dict(SCAN_HOOK_RESULT)


def _default_safe_map(keys, links):
    return {str(key): True for key in (keys or [])}


def _default_recheck(*args, **kwargs):
    return True, ""


stub_guard = _new_module(
    "guard",
    plan_cycle=_default_plan_cycle,
    build_scan=_default_build_scan,
    safe_delete_files_map=_default_safe_map,
    recheck_torrent_action=_default_recheck,
    recheck_media_action=_default_recheck,
    is_ignored=lambda torrent, config: None,
    action_id_for=lambda kind, keys: f"{kind}-{'-'.join(sorted(str(k) for k in (keys or [])))}",
)

stub_linker = _new_module("linker")


def _default_link_hook(media_items, torrents, history_rows=None):
    LINK_CALLS.append(
        {"media": list(media_items or []), "torrents": list(torrents or []), "history": history_rows}
    )
    from models import LinkIndex  # noqa: WPS433 - 测试内延迟导入

    return SimpleNamespace(index=LinkIndex(), stats=SimpleNamespace(to_dict=lambda: {}))


LINK_HOOK = _default_link_hook


class _StubLinkBuilder:
    def build(self, media_items, torrents, history_rows=None):
        return LINK_HOOK(media_items, torrents, history_rows)


stub_linker.LinkBuilder = _StubLinkBuilder

import clients as clients_module  # noqa: E402
import runtime as runtime_module  # noqa: E402

from config import PluginConfig  # noqa: E402
from models import LinkIndex, MediaItem, PendingAction, Snapshot, TorrentItem  # noqa: E402


# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------
class FakeJellyfin:
    def __init__(self, name="Jellyfin", items=None):
        self.name = name
        self.items = items or []
        self.fail: str = ""
        self.deleted = []
        self.delete_ok = True
        self.test_result = (True, "Jellyfin 连接成功")

    def list_media_items(self):
        if self.fail:
            raise clients_module.ClientError(self.fail)
        return [MediaItem.from_dict(item.to_dict()) for item in self.items], []

    def delete_item(self, item_id):
        if not self.delete_ok:
            return False, "HTTP 500", 500
        self.deleted.append(item_id)
        return True, "", 204

    def test(self):
        return self.test_result


class FakeQbittorrent:
    def __init__(self, name="主QB", torrents=None):
        self.name = name
        self.torrents = torrents or []
        self.fail: str = ""
        self.delete_ok = True
        self.delete_calls = []
        self.test_result = (True, "qBittorrent 连接成功")

    def list_torrents(self):
        if self.fail:
            raise clients_module.ClientError(self.fail)
        return [TorrentItem.from_dict(item.to_dict()) for item in self.torrents], []

    def delete_torrents(self, hashes, delete_files):
        self.delete_calls.append({"hashes": list(hashes), "delete_files": bool(delete_files)})
        return self.delete_ok, "" if self.delete_ok else "删除失败"

    def test(self):
        return self.test_result


class FakePlugin:
    def __init__(self, data_dir):
        self._data_dir = pathlib.Path(data_dir)
        self.notifications = []
        self.post_messages = []

    def get_data_path(self):
        return self._data_dir

    def _notify(self, title, text):
        self.notifications.append((title, text))

    def post_message(self, **kwargs):
        self.post_messages.append(kwargs)


# ---------------------------------------------------------------------------
# app.db 桩（历史清理）
# ---------------------------------------------------------------------------
DB_CALLS = {"transfer_list_by_hash": [], "transfer_deleted": [], "transfer_dest": [], "download_deleted": []}


def _install_db_stubs():
    app = sys.modules["app"]
    app.db = _new_module("app.db")
    models_module = _new_module("app.db.models")
    transfer_models = _new_module("app.db.models.transferhistory")

    class _FakeTransferHistory:
        records = []
        list_by_page_calls = 0

        @classmethod
        def list_by_page(cls, db, page=1, count=30, status=None):
            cls.list_by_page_calls += 1
            return list(cls.records)

    transfer_models.TransferHistory = _FakeTransferHistory
    models_module.transferhistory = transfer_models

    transfer_oper = _new_module("app.db.transferhistory_oper")

    class _FakeTransferHistoryOper:
        def list_by_hash(self, torrent_hash):
            DB_CALLS["transfer_list_by_hash"].append(torrent_hash)
            return [SimpleNamespace(id=len(DB_CALLS["transfer_list_by_hash"]))]

        def delete(self, history_id):
            DB_CALLS["transfer_deleted"].append(history_id)

        def get_by_dest(self, dest):
            DB_CALLS["transfer_dest"].append(dest)
            return SimpleNamespace(id=101)

        def get_by(self, dest=None):
            return []

    transfer_oper.TransferHistoryOper = _FakeTransferHistoryOper
    app.db.models = models_module
    app.db.transferhistory_oper = transfer_oper

    download_oper = _new_module("app.db.downloadhistory_oper")

    class _FakeDownloadHistoryOper:
        def get_by_hash(self, torrent_hash):
            return SimpleNamespace(id=201)

        def delete_history(self, history_id):
            DB_CALLS["download_deleted"].append(history_id)

    download_oper.DownloadHistoryOper = _FakeDownloadHistoryOper
    app.db.downloadhistory_oper = download_oper


def _reset_db_calls():
    for key in DB_CALLS:
        DB_CALLS[key].clear()


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------
class RuntimeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-runtime-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_root = self.root / "medias"
        self.media_root.mkdir(parents=True, exist_ok=True)
        (self.media_root / "Downloads").mkdir(parents=True, exist_ok=True)

        LOG_RECORDS.clear()
        PLAN_CALLS.clear()
        LINK_CALLS.clear()
        _reset_db_calls()

        # 重置核心模块桩
        stub_guard.plan_cycle = _default_plan_cycle
        stub_guard.build_scan = _default_build_scan
        stub_guard.safe_delete_files_map = _default_safe_map
        stub_guard.recheck_torrent_action = _default_recheck
        stub_guard.recheck_media_action = _default_recheck
        global LINK_HOOK
        LINK_HOOK = _default_link_hook
        SCAN_HOOK_RESULT.clear()

        self.jellyfin = FakeJellyfin()
        self.qb = FakeQbittorrent()
        self.bundle = SimpleNamespace(
            jellyfin=[self.jellyfin], qbittorrent=[self.qb], warnings=[]
        )
        self.bundle_factory = lambda config: self.bundle
        runtime_module.build_client_bundle = self.bundle_factory
        self.plugin = FakePlugin(self.data_dir)
        self.config = PluginConfig(
            enabled=True,
            dry_run=True,
            notify=True,
            grace_seconds=0,
            media_delete_roots=[str(self.media_root)],
            download_delete_roots=[str(self.media_root / "Downloads")],
            clean_moviepilot_history=False,
            keep_history_actions=50,
        )
        self.runtime = self._new_runtime(self.config)

    def tearDown(self):
        try:
            self.runtime.stop()
        except Exception:  # noqa: BLE001
            pass
        runtime_module.build_client_bundle = clients_module.build_client_bundle
        self._tmp.cleanup()

    def _new_runtime(self, config):
        runtime = runtime_module.SyncRuntime(plugin=self.plugin, config=config)
        runtime.configure(config)
        return runtime

    @staticmethod
    def _media(item_id, path="", inode=0, dev=0, item_type="Movie", name=""):
        return MediaItem(
            server="Jellyfin",
            item_id=item_id,
            path=path or f"/media/{item_id}.mkv",
            inode=inode,
            dev=dev,
            item_type=item_type,
            name=name or item_id,
        )

    @staticmethod
    def _torrent(torrent_hash="abc", downloader="主QB", content_path="/medias/Downloads/T"):
        return TorrentItem(
            downloader=downloader,
            hash=torrent_hash,
            name="Test.Torrent",
            save_path="/medias/Downloads",
            content_path=content_path,
            progress=1.0,
            size=100,
        )

    @staticmethod
    def _action(kind="delete_torrents", action_id="act-1", due_at=None, dry_run=False, payload=None):
        now = time.time()
        return PendingAction(
            action_id=action_id,
            kind=kind,
            created_at=now,
            due_at=now if due_at is None else due_at,
            reason="测试动作",
            payload=payload
            or (
                {"torrents": [{"downloader": "主QB", "hash": "abc", "delete_files": True}]}
                if kind == "delete_torrents"
                else {
                    "items": [
                        {
                            "server": "Jellyfin",
                            "item_id": "m1",
                            "path": "/medias/m1.mkv",
                            "inode": 1,
                            "dev": 1,
                        }
                    ]
                }
            ),
            dry_run=dry_run,
        )

    @staticmethod
    def _candidate(data):
        return SimpleNamespace(to_dict=lambda: dict(data))

    @staticmethod
    def _plan(actions=None, torrent_candidates=None, media_candidates=None, notices=None):
        return SimpleNamespace(
            torrent_candidates=torrent_candidates or [],
            media_candidates=media_candidates or [],
            actions=actions or [],
            notices=notices or [],
            summary={},
        )


# ---------------------------------------------------------------------------
# 配置与状态结构
# ---------------------------------------------------------------------------
class ConfigureStatusTest(RuntimeTestBase):
    def test_status_contract_shape(self):
        status = self.runtime.get_status()
        self.assertIsInstance(status, dict)
        expected_keys = {
            "enabled",
            "dry_run",
            "configured",
            "warnings",
            "selected_downloaders",
            "selected_media_servers",
            "auto",
            "last_poll",
            "pending",
            "candidates",
            "scan",
            "last_error",
        }
        self.assertTrue(expected_keys.issubset(status.keys()))
        self.assertTrue(status["enabled"])
        self.assertTrue(status["dry_run"])
        self.assertTrue(status["configured"])
        self.assertEqual(status["selected_downloaders"], ["主QB"])
        self.assertEqual(status["selected_media_servers"], ["Jellyfin"])
        self.assertEqual(status["auto"], {"media_to_torrent": False, "torrent_to_media": False})
        self.assertEqual(status["pending"], {"count": 0, "dry_run_count": 0, "next_due_at": 0.0})
        self.assertEqual(status["candidates"], {"torrents": [], "media": []})
        self.assertEqual(
            status["scan"], {"running": False, "has_result": False, "last_generated_at": 0.0}
        )
        self.assertEqual(status["last_error"], "")
        for key in (
            "at",
            "duration",
            "ok",
            "partial",
            "errors",
            "media_total",
            "torrent_total",
            "links",
            "lost_media",
            "lost_torrents",
            "actions_created",
            "actions_executed",
        ):
            self.assertIn(key, status["last_poll"])

    def test_configure_failure_does_not_raise(self):
        def _boom(config):
            raise clients_module.ClientError("未找到可用的服务")

        runtime_module.build_client_bundle = _boom
        config = self.config.merged(downloaders=["不存在"], media_servers=["不存在"])
        runtime = runtime_module.SyncRuntime(plugin=self.plugin, config=config)
        runtime.configure(config)  # 不应抛出异常
        status = runtime.get_status()
        self.assertFalse(status["configured"])
        self.assertTrue(status["warnings"])
        self.assertIn("未找到可用的服务", status["last_error"])
        last_poll = runtime.poll_once()
        self.assertFalse(last_poll["ok"])
        self.assertTrue(last_poll["partial"])
        self.assertTrue(last_poll["errors"])

    def test_configure_accepts_plain_dict(self):
        runtime = runtime_module.SyncRuntime(plugin=self.plugin, config={"enabled": True})
        runtime.configure({"enabled": True, "dry_run": False})
        status = runtime.get_status()
        self.assertTrue(status["enabled"])
        self.assertFalse(status["dry_run"])


# ---------------------------------------------------------------------------
# 轮询：基线、失败降级、通知
# ---------------------------------------------------------------------------
class PollTest(RuntimeTestBase):
    def test_first_poll_creates_baseline_and_persists(self):
        self.jellyfin.items = [self._media("m1"), self._media("m2")]
        self.qb.torrents = [self._torrent("abc")]
        summary = self.runtime.poll_once()
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["partial"])
        self.assertEqual(summary["media_total"], 2)
        self.assertEqual(summary["torrent_total"], 1)
        self.assertEqual(summary["lost_media"], 0)
        self.assertEqual(summary["actions_created"], 0)
        self.assertEqual(self.runtime.get_status()["pending"]["count"], 0)
        # 状态已持久化：新实例加载后 last_poll.at 与快照数量一致
        runtime2 = self._new_runtime(self.config)
        status2 = runtime2.get_status()
        self.assertEqual(status2["last_poll"]["at"], summary["at"])
        self.assertEqual(len(runtime2._snapshot.media), 2)

    def test_partial_fetch_merges_previous_and_skips_planning(self):
        self.jellyfin.items = [self._media("m1"), self._media("m2")]
        self.qb.torrents = [self._torrent("abc")]
        self.runtime.poll_once()
        PLAN_CALLS.clear()
        # 第二次抓取：Jellyfin 失败，qB 正常
        self.jellyfin.fail = "connection refused"
        self.qb.torrents = [self._torrent("abc")]
        summary = self.runtime.poll_once()
        self.assertTrue(summary["partial"])
        self.assertFalse(summary["ok"])
        self.assertTrue(any("Jellyfin" in error for error in summary["errors"]))
        # 失败来源被旧快照补齐：不会产生 lost_media，也不会生成动作
        self.assertEqual(summary["lost_media"], 0)
        self.assertEqual(summary["actions_created"], 0)
        self.assertEqual(PLAN_CALLS, [])
        current = self.runtime._snapshot
        self.assertEqual(set(current.media.keys()), {"Jellyfin|m1", "Jellyfin|m2"})
        # 通知里包含出错摘要
        self.assertTrue(
            any("运行出错" in text for _title, text in self.plugin.notifications)
        )

    def test_partial_fetch_does_not_execute_pending(self):
        self.runtime._pending = [self._action()]
        self.jellyfin.fail = "timeout"
        summary = self.runtime.poll_once()
        self.assertTrue(summary["partial"])
        self.assertEqual(self.qb.delete_calls, [])
        self.assertEqual(len(self.runtime._pending), 1)
        self.assertEqual(summary["actions_executed"], 0)

    def test_plan_receives_previous_and_current_views(self):
        self.jellyfin.items = [self._media("m1"), self._media("m2")]
        self.qb.torrents = [self._torrent("abc")]
        self.runtime.poll_once()
        self.jellyfin.items = [self._media("m1")]
        self.runtime.poll_once()
        self.assertEqual(len(PLAN_CALLS), 2)  # 首轮建基线 + 本轮
        args = PLAN_CALLS[-1]["args"]
        diff, previous, current = args[1], args[2], args[3]
        self.assertEqual(len(diff.lost_media), 1)
        self.assertEqual(diff.lost_media[0].item_id, "m2")
        self.assertIn("Jellyfin|m2", previous.media)
        self.assertNotIn("Jellyfin|m2", current.media)

    def test_candidates_and_dry_run_notification(self):
        self.jellyfin.items = [self._media("m1")]
        self.qb.torrents = [self._torrent("abc")]
        self.runtime.poll_once()
        dry_action = self._action(
            action_id="torrents-dry",
            dry_run=True,
            due_at=time.time() + 300,
            payload={"torrents": [{"downloader": "主QB", "hash": "abc", "delete_files": False}]},
        )
        media_candidate = self._candidate(
            {
                "server": "Jellyfin",
                "item_id": "m1",
                "name": "m1",
                "reason": "torrent_deleted",
                "blocked_reason": "torrent_still_shared",
                "auto_eligible": False,
            }
        )
        info_candidate = self._candidate(
            {
                "server": "Jellyfin",
                "item_id": "m2",
                "name": "m2",
                "reason": "torrent_deleted",
                "blocked_reason": "media_already_gone",
                "auto_eligible": False,
            }
        )
        torrent_candidate = self._candidate(
            {
                "hash": "abc",
                "downloader": "主QB",
                "name": "T",
                "reason": "media_deleted",
                "blocked_reason": "",
                "auto_eligible": True,
            }
        )
        stub_guard.plan_cycle = lambda *a, **k: self._plan(
            actions=[dry_action],
            media_candidates=[media_candidate, info_candidate],
            torrent_candidates=[torrent_candidate],
            notices=["检测到 10 个媒体条目同时消失，已跳过自动删种"],
        )
        self.jellyfin.items = []
        summary = self.runtime.poll_once()
        self.assertEqual(summary["actions_created"], 1)
        status = self.runtime.get_status()
        self.assertEqual(status["pending"]["count"], 1)
        self.assertEqual(status["pending"]["dry_run_count"], 1)
        self.assertEqual(len(status["candidates"]["torrents"]), 1)
        # 信息型媒体候选被过滤
        self.assertEqual(len(status["candidates"]["media"]), 1)
        self.assertEqual(status["candidates"]["media"][0]["blocked_reason"], "torrent_still_shared")
        dry_notifications = [
            text for title, text in self.plugin.notifications if "演练" in title
        ]
        self.assertTrue(dry_notifications)
        self.assertIn("未执行删除", dry_notifications[0])
        batch_notifications = [
            text
            for _title, text in self.plugin.notifications
            if "批量" in text or "同时消失" in text
        ]
        self.assertEqual(len(batch_notifications), 1)

    def test_link_index_reused_when_content_unchanged(self):
        self.jellyfin.items = [self._media("m1"), self._media("m2")]
        self.qb.torrents = [self._torrent("abc")]
        self.runtime.poll_once()
        self.assertEqual(len(LINK_CALLS), 1)
        # 内容不变：复用索引，不重复遍历磁盘
        self.runtime.poll_once()
        self.assertEqual(len(LINK_CALLS), 1)
        # 内容变化：必须重建索引
        self.jellyfin.items = [self._media("m1")]
        self.runtime.poll_once()
        self.assertEqual(len(LINK_CALLS), 2)

    def test_scheduled_poll_skips_when_busy_or_disabled(self):
        # 插件停用：不轮询
        config = self.config.merged(enabled=False)
        runtime = self._new_runtime(config)
        self.jellyfin.items = [self._media("m1")]
        runtime.scheduled_poll()
        self.assertEqual(runtime._last_poll["at"], 0.0)
        # 轮询锁被占用：跳过本轮
        self.assertTrue(runtime._poll_lock.acquire(blocking=False))
        try:
            runtime.scheduled_poll()
        finally:
            runtime._poll_lock.release()
        self.assertEqual(runtime._last_poll["at"], 0.0)

    def test_trigger_poll_busy_returns_message(self):
        self.runtime._last_poll = {"at": 123.0, "ok": True}
        self.assertTrue(self.runtime._poll_lock.acquire(blocking=False))
        try:
            result = self.runtime.trigger_poll()
        finally:
            self.runtime._poll_lock.release()
        self.assertTrue(result.get("skipped"))
        self.assertIn("尚未结束", result.get("message", ""))


# ---------------------------------------------------------------------------
# 待执行动作执行
# ---------------------------------------------------------------------------
class ExecuteActionsTest(RuntimeTestBase):
    def _clean_config(self):
        return self.config.merged(
            dry_run=False,
            auto_media_to_torrent=True,
            clean_moviepilot_history=True,
        )

    def test_execute_torrent_action_success(self):
        _install_db_stubs()
        self.qb.torrents = [self._torrent("abc")]
        runtime = self._new_runtime(self._clean_config())
        runtime._pending = [self._action()]
        summary = runtime.poll_once()
        self.assertEqual(summary["actions_executed"], 1)
        self.assertEqual(len(self.qb.delete_calls), 1)
        self.assertEqual(self.qb.delete_calls[0]["hashes"], ["abc"])
        self.assertTrue(self.qb.delete_calls[0]["delete_files"])
        # 下载任务已从内存快照移除
        self.assertNotIn("主QB|abc", runtime._snapshot.torrents)
        # 历史记录清理被调用
        self.assertEqual(DB_CALLS["transfer_list_by_hash"], ["abc"])
        self.assertEqual(DB_CALLS["transfer_deleted"], [1])
        self.assertEqual(DB_CALLS["download_deleted"], [201])
        # 动作归档到 history
        pending = runtime.get_pending()
        self.assertEqual(pending["pending"], [])
        self.assertEqual(pending["history"][0]["status"], "done")
        self.assertTrue(
            any("已删除 1 个种子" in text for _title, text in self.plugin.notifications)
        )

    def test_execute_media_action_success_with_sidecar_cleanup(self):
        _install_db_stubs()
        self.qb.torrents = [self._torrent("keep")]
        runtime = self._new_runtime(self._clean_config().merged(auto_torrent_to_media=True))
        movie = self.media_root / "Movie" / "movie.mkv"
        movie.parent.mkdir(parents=True, exist_ok=True)
        movie.write_bytes(b"video")
        sidecar = movie.parent / "movie.srt"
        sidecar.write_bytes(b"sub")
        entry = {
            "server": "Jellyfin",
            "item_id": "m1",
            "path": str(movie),
            "name": "movie",
            "item_type": "Movie",
            "inode": int(movie.stat().st_ino),
            "dev": int(movie.stat().st_dev),
        }
        runtime._snapshot = Snapshot(
            media={
                "Jellyfin|m1": MediaItem.from_dict(entry),
            }
        )
        runtime._pending = [
            PendingAction(
                action_id="media-1",
                kind="delete_media",
                created_at=time.time(),
                due_at=time.time(),
                reason="测试",
                payload={"items": [entry]},
            )
        ]
        summary = runtime.poll_once()
        self.assertEqual(summary["actions_executed"], 1)
        self.assertEqual(self.jellyfin.deleted, ["m1"])
        self.assertFalse(sidecar.exists())
        self.assertEqual(DB_CALLS["transfer_dest"], [str(movie)])

    def test_delete_files_safety_enforced(self):
        config = self._clean_config()
        runtime = self._new_runtime(config)
        stub_guard.safe_delete_files_map = lambda keys, links: {str(key): False for key in keys or []}
        runtime._pending = [self._action()]
        runtime.poll_once()
        self.assertEqual(len(self.qb.delete_calls), 1)
        self.assertFalse(self.qb.delete_calls[0]["delete_files"])
        history = runtime.get_pending()["history"]
        self.assertEqual(history[0]["status"], "done")
        # 执行删除后索引已过期，下一轮必须重建
        self.assertIsNone(runtime._links_signature)

    def test_recheck_failure_skips_without_deleting(self):
        runtime = self._new_runtime(self._clean_config())
        stub_guard.recheck_torrent_action = lambda *a, **k: (False, "种子仍有现存媒体关联")
        runtime._pending = [self._action()]
        summary = runtime.poll_once()
        self.assertEqual(summary["actions_executed"], 0)
        self.assertEqual(self.qb.delete_calls, [])
        history = runtime.get_pending()["history"]
        self.assertEqual(history[0]["status"], "skipped")
        self.assertIn("复核未通过", history[0]["summary"])

    def test_auto_switch_off_keeps_pending(self):
        runtime = self._new_runtime(self.config.merged(dry_run=False))
        runtime._pending = [self._action()]
        runtime.poll_once()
        self.assertEqual(self.qb.delete_calls, [])
        self.assertEqual(len(runtime.get_pending()["pending"]), 1)

    def test_dry_run_config_keeps_real_pending_unexecuted(self):
        runtime = self._new_runtime(self.config.merged(dry_run=True))
        runtime._pending = [self._action(dry_run=False)]
        runtime.poll_once()
        self.assertEqual(self.qb.delete_calls, [])
        self.assertEqual(len(runtime.get_pending()["pending"]), 1)

    def test_dry_run_action_is_skipped_after_due(self):
        runtime = self._new_runtime(self.config.merged(dry_run=True, auto_media_to_torrent=True))
        runtime._pending = [self._action(dry_run=True)]
        runtime.poll_once()
        self.assertEqual(self.qb.delete_calls, [])
        pending = runtime.get_pending()
        self.assertEqual(pending["pending"], [])
        self.assertEqual(pending["history"][0]["status"], "skipped")
        self.assertIn("演练", pending["history"][0]["summary"])

    def test_startup_drops_dry_run_pending_when_config_is_real_mode(self):
        # 以演练模式写入一条 dry_run 待办并持久化
        self.runtime._pending = [
            self._action(action_id="dry-startup", dry_run=True, due_at=time.time() + 999)
        ]
        self.runtime._persist(force=True)
        # 模拟重启进程：新实例使用“关闭演练”的配置启动
        runtime2 = self._new_runtime(self.config.merged(dry_run=False))
        pending = runtime2.get_pending()
        self.assertEqual(pending["pending"], [])
        self.assertEqual(pending["history"][0]["status"], "cancelled")
        self.assertIn("演练", pending["history"][0]["summary"])

    def test_dry_run_switch_off_drops_old_dry_run_actions(self):
        dry_action = self._action(action_id="dry-1", dry_run=True)
        self.runtime._pending = [dry_action]
        config = self.config.merged(dry_run=False)
        self.runtime.configure(config)
        pending = self.runtime.get_pending()
        self.assertEqual(pending["pending"], [])
        self.assertEqual(pending["history"][0]["status"], "cancelled")


# ---------------------------------------------------------------------------
# 扫描与手动应用
# ---------------------------------------------------------------------------
class ScanAndApplyTest(RuntimeTestBase):
    def _run_scan(self, result):
        SCAN_HOOK_RESULT.clear()
        SCAN_HOOK_RESULT.update(result)
        stub_guard.build_scan = lambda *args, **kwargs: dict(SCAN_HOOK_RESULT)
        started = self.runtime.start_scan()
        self.assertTrue(started["started"], started)
        thread = self.runtime._scan_thread
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "扫描线程未在超时内结束")
        return self.runtime.get_scan_state()

    def _media_row(self, media, item_id="m1", inode=None, dev=None):
        stat_result = media.stat()
        return {
            "id": f"Jellyfin|{item_id}",
            "server": "Jellyfin",
            "item_id": item_id,
            "name": media.stem,
            "display_title": media.stem,
            "item_type": "Movie",
            "series_name": "",
            "season": 0,
            "episode": 0,
            "path": str(media),
            "folder": str(media.parent),
            "size": 1,
            "inode": stat_result.st_ino if inode is None else inode,
            "dev": stat_result.st_dev if dev is None else dev,
            "history_hashes": [],
            "reason": "no_torrent_link",
        }

    def _torrent_row(self, torrent_hash="abc", downloader="主QB", ignored_reason=""):
        return {
            "id": f"{downloader}|{torrent_hash}",
            "downloader": downloader,
            "hash": torrent_hash,
            "name": "Torrent",
            "category": "TV",
            "tags": [],
            "state": "stalledUP",
            "progress": 1.0,
            "size": 1,
            "save_path": "/medias/Downloads",
            "content_path": "/medias/Downloads/T",
            "added_on": 1,
            "completion_on": 1,
            "completed": True,
            "video_files": 1,
            "path_exists": True,
            "reason": "no_media_link",
            "ignored_reason": ignored_reason,
            "shared_torrents": 0,
            "safe_to_delete_files": True,
        }

    def _seed_live_state(self, media_rows=None, torrent_rows=None):
        """把扫描行对应的实时快照/门控状态写入 runtime（apply 会按实时状态复核）。"""
        media = {}
        for row in media_rows or []:
            media[row["id"]] = MediaItem.from_dict(row)
        torrents = {}
        for row in torrent_rows or []:
            item = TorrentItem.from_dict(
                {
                    "downloader": row.get("downloader", "主QB"),
                    "hash": row.get("hash", ""),
                    "name": row.get("name", ""),
                    "category": row.get("category", ""),
                    "tags": row.get("tags") or [],
                    "state": row.get("state", ""),
                    "progress": row.get("progress", 1.0),
                    "size": row.get("size", 0),
                    "save_path": row.get("save_path", ""),
                    "content_path": row.get("content_path", ""),
                    "path_exists": row.get("path_exists", True),
                }
            )
            torrents[item.key] = item
        self.runtime._snapshot = Snapshot(media=media, torrents=torrents)
        self.runtime._last_poll = {"at": time.time(), "ok": True}
        self.runtime._coverage_complete = True
        self.runtime._downloaders_verifiable = True
        self.runtime._file_deletion_safe = True
        self.runtime._exec_config = self.runtime._config

    def _result(self, media_rows=None, torrent_rows=None):
        return {
            "generated_at": time.time(),
            "duration": 0.5,
            "stats": {"media_total": 1, "torrent_total": 1, "link_pairs": 0},
            "media_without_torrent": media_rows or [],
            "media_groups": [],
            "torrents_without_media": torrent_rows or [],
            "torrent_groups": [],
            "skipped": {"downloading": 0, "no_video": 0, "ignored": 0, "missing_path": 0, "unsupported": 0},
        }

    def test_scan_success_state_shell(self):
        movie = self.media_root / "Movie" / "movie.mkv"
        movie.parent.mkdir(parents=True, exist_ok=True)
        movie.write_bytes(b"video")
        result = self._result(media_rows=[self._media_row(movie)])
        state = self._run_scan(result)
        self.assertFalse(state["running"])
        self.assertEqual(state["progress"], "完成")
        self.assertEqual(state["error"], "")
        self.assertIsNotNone(state["result"])
        self.assertEqual(state["result"]["stats"]["media_total"], 1)
        self.assertEqual(
            self.runtime.get_status()["scan"],
            {
                "running": False,
                "has_result": True,
                "last_generated_at": state["result"]["generated_at"],
            },
        )

    def test_scan_failure_records_error(self):
        self.jellyfin.fail = "scan timeout"
        started = self.runtime.start_scan()
        self.assertTrue(started["started"])
        thread = self.runtime._scan_thread
        thread.join(timeout=5)
        state = self.runtime.get_scan_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["progress"], "失败")
        self.assertIn("scan timeout", state["error"])
        self.assertIsNone(state["result"])

    def test_scan_requires_clients(self):
        runtime_module.build_client_bundle = self.bundle_factory
        config = self.config.merged(media_servers=["Jellyfin"], downloaders=["主QB"])
        runtime = self._new_runtime(config)
        runtime._clients = None
        runtime._configured = False
        result = runtime.start_scan()
        self.assertFalse(result["started"])
        self.assertTrue(result["message"])

    def test_apply_media_success(self):
        movie = self.media_root / "Movie" / "movie.mkv"
        movie.parent.mkdir(parents=True, exist_ok=True)
        movie.write_bytes(b"video")
        sidecar = movie.parent / "movie.srt"
        sidecar.write_bytes(b"sub")
        row = self._media_row(movie)
        self._run_scan(self._result(media_rows=[row]))
        self._seed_live_state(media_rows=[row])
        response = self.runtime.apply_scan_selection(
            {"kind": "media", "ids": [row["id"]], "delete_sidecars": True, "cleanup_empty_dirs": True}
        )
        self.assertTrue(response["success"], response)
        self.assertEqual(response["deleted"], 1)
        self.assertEqual(response["failed"], [])
        self.assertEqual(self.jellyfin.deleted, ["m1"])
        self.assertFalse(sidecar.exists())
        # 已删除条目从扫描结果中移除
        result = self.runtime.get_scan_state()["result"]
        self.assertEqual(result["media_without_torrent"], [])

    def test_apply_rejects_unknown_and_unsafe_ids(self):
        movie = self.media_root / "Movie" / "movie.mkv"
        movie.parent.mkdir(parents=True, exist_ok=True)
        movie.write_bytes(b"video")
        row = self._media_row(movie)
        self._run_scan(self._result(media_rows=[row]))
        self._seed_live_state(media_rows=[row])
        response = self.runtime.apply_scan_selection(
            {"kind": "media", "ids": ["Jellyfin|not-in-scan"]}
        )
        self.assertFalse(response["success"])
        self.assertEqual(len(response["failed"]), 1)
        self.assertEqual(self.jellyfin.deleted, [])

        # 媒体 inode 仍被现存种子引用 -> 拒绝
        index = LinkIndex()
        index.add_torrent_inode((int(row["dev"]), int(row["inode"])), self._torrent("abc"))
        self.runtime._links = index
        response = self.runtime.apply_scan_selection({"kind": "media", "ids": [row["id"]]})
        self.assertFalse(response["success"])
        self.assertTrue(response["warnings"])
        self.assertEqual(self.jellyfin.deleted, [])

    def test_apply_media_outside_delete_roots_rejected(self):
        outside = self.root / "outside" / "movie.mkv"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"video")
        row = self._media_row(outside)
        result = self._result(media_rows=[row])
        self._run_scan(result)
        self._seed_live_state(media_rows=[row])
        response = self.runtime.apply_scan_selection(
            {"kind": "media", "ids": [f"Jellyfin|m1"]}
        )
        self.assertFalse(response["success"])
        self.assertIn("允许删除", response["failed"][0]["error"])

    def test_apply_torrents_success_and_ignored(self):
        rows = [self._torrent_row("abc"), self._torrent_row("def", ignored_reason="ignored_category")]
        self._run_scan(self._result(torrent_rows=rows))
        self._seed_live_state(torrent_rows=rows)
        response = self.runtime.apply_scan_selection(
            {"kind": "torrents", "ids": ["主QB|abc", "主QB|def"], "delete_files": False}
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["deleted"], 1)
        self.assertEqual(len(response["failed"]), 1)
        self.assertEqual(self.qb.delete_calls, [{"hashes": ["abc"], "delete_files": False}])
        self.assertTrue(any("忽略规则" in warning for warning in response["warnings"]))

    def test_apply_torrents_forces_delete_files_off_when_unsafe(self):
        rows = [self._torrent_row("abc")]
        self._run_scan(self._result(torrent_rows=rows))
        self._seed_live_state(torrent_rows=rows)
        stub_guard.safe_delete_files_map = lambda keys, links: {str(key): False for key in keys or []}
        response = self.runtime.apply_scan_selection(
            {"kind": "torrents", "ids": ["主QB|abc"], "delete_files": True}
        )
        self.assertTrue(response["success"])
        self.assertEqual(self.qb.delete_calls[0]["delete_files"], False)
        self.assertTrue(response["warnings"])

    def test_apply_without_scan_result(self):
        response = self.runtime.apply_scan_selection({"kind": "media", "ids": ["Jellyfin|m1"]})
        self.assertFalse(response["success"])
        self.assertIn("扫描", response["message"])
        # 未知 kind
        self._run_scan(self._result())
        response = self.runtime.apply_scan_selection({"kind": "bogus", "ids": ["x"]})
        self.assertFalse(response["success"])
        self.assertTrue(response["warnings"])


# ---------------------------------------------------------------------------
# 待办与连接测试
# ---------------------------------------------------------------------------
class PendingAndConnectionsTest(RuntimeTestBase):
    def test_get_pending_targets_and_cancel(self):
        runtime = self.runtime
        action = self._action(action_id="act-1")
        media_action = PendingAction(
            action_id="act-2",
            kind="delete_media",
            created_at=time.time(),
            due_at=time.time() + 100,
            reason="测试",
            payload={"items": [{"server": "Jellyfin", "item_id": "m1"}]},
            dry_run=True,
        )
        runtime._pending = [action, media_action]
        pending = runtime.get_pending()
        self.assertEqual(len(pending["pending"]), 2)
        targets = {item["action_id"]: item["targets"] for item in pending["pending"]}
        self.assertEqual(targets["act-1"], ["主QB|abc"])
        self.assertEqual(targets["act-2"], ["Jellyfin|m1"])
        self.assertEqual(runtime.get_status()["pending"]["count"], 2)
        self.assertEqual(runtime.cancel_pending("act-1"), 1)
        self.assertEqual(runtime.cancel_pending(), 1)
        self.assertEqual(runtime.cancel_pending(), 0)
        self.assertEqual(runtime.get_pending()["pending"], [])

    def test_history_limited_by_keep_history_actions(self):
        config = self.config.merged(keep_history_actions=2)
        runtime = self._new_runtime(config)
        for index in range(4):
            action = self._action(action_id=f"act-{index}", due_at=time.time() + 100)
            runtime._pending.append(action)
            runtime.cancel_pending(f"act-{index}")
        self.assertEqual(len(runtime.get_pending()["history"]), 2)

    def test_test_connections(self):
        result = self.runtime.test_connections()
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 2)
        self.assertIn("连接正常", result["message"])

        self.qb.test_result = (False, "qBittorrent 连接失败")
        result = self.runtime.test_connections()
        self.assertFalse(result["success"])
        self.assertIn("部分", result["message"])

        self.jellyfin.test_result = (False, "Jellyfin 连接失败")
        self.qb.test_result = (False, "qBittorrent 连接失败")
        result = self.runtime.test_connections()
        self.assertFalse(result["success"])
        self.assertIn("所有", result["message"])

        runtime = self._new_runtime(self.config)
        runtime._clients = None
        result = runtime.test_connections()
        self.assertFalse(result["success"])
        self.assertEqual(result["results"], [])

    def test_history_rows_cached(self):
        _install_db_stubs()
        from app.db.models import transferhistory as transfer_models  # noqa: E402

        record = SimpleNamespace(
            downloader="主QB", download_hash="abc", dest=str(self.media_root / "x.mkv")
        )
        transfer_models.TransferHistory.records = [record]
        transfer_models.TransferHistory.list_by_page_calls = 0
        runtime = self._new_runtime(self.config)
        rows = runtime._read_history_rows()
        self.assertEqual(rows, [("主QB", "abc", str(self.media_root / "x.mkv"))])
        runtime._read_history_rows()
        self.assertEqual(transfer_models.TransferHistory.list_by_page_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
