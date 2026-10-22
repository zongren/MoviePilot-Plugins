"""MediaSyncGuard ``state.py`` 单元测试（仅标准库）。

覆盖：快照 diff 的改名/inode 保护、种子消失检测、持久化状态读写、
损坏文件/版本不兼容降级、原子保存、失败不抛异常。

运行：``python3 plugins.v2/mediasyncguard/tests/test_state.py``
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
import tempfile
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from models import (  # noqa: E402
    MediaItem,
    PendingAction,
    Snapshot,
    TorrentItem,
)
from state import (  # noqa: E402
    DATA_VERSION,
    STATE_FILENAME,
    PersistedState,
    SnapshotDiff,
    StateStore,
    diff_snapshots,
)


def make_media(
    item_id: str = "m1",
    path: str = "/medias/movies/a.mkv",
    inode: int = 1001,
    dev: int = 7,
    server: str = "Jellyfin",
) -> MediaItem:
    return MediaItem(server=server, item_id=item_id, path=path, inode=inode, dev=dev)


def make_torrent(hash_: str = "h1", downloader: str = "主QB") -> TorrentItem:
    return TorrentItem(downloader=downloader, hash=hash_)


def snapshot(media=(), torrents=()) -> Snapshot:
    return Snapshot(
        media={item.key: item for item in media},
        torrents={item.key: item for item in torrents},
    )


class DiffSnapshotsTest(unittest.TestCase):
    def test_previous_none_returns_empty_diff(self):
        current = snapshot(media=[make_media()], torrents=[make_torrent()])
        before = Snapshot(created_at=1)
        diff = diff_snapshots(None, current)
        self.assertIsInstance(diff, SnapshotDiff)
        self.assertFalse(diff.lost_media)
        self.assertFalse(diff.lost_torrents)
        self.assertEqual(diff.new_media_count, 0)
        self.assertEqual(diff.new_torrent_count, 0)
        self.assertTrue(diff.is_empty)
        # 不修改入参
        self.assertEqual(before.media, {})

    def test_lost_media_detected(self):
        lost = make_media(item_id="m1", inode=1001, path="/medias/m1.mkv")
        keep = make_media(item_id="m2", inode=1002, path="/medias/m2.mkv")
        previous = snapshot(media=[lost, keep])
        current = snapshot(media=[keep])
        diff = diff_snapshots(previous, current)
        self.assertEqual([item.key for item in diff.lost_media], [lost.key])

    def test_rename_protected_by_same_inode(self):
        # 媒体库重扫导致 item_id 变化 / 改名，但 inode 未变 -> 绝不能算丢失
        before = make_media(item_id="old-id", path="/medias/movies/old.mkv", inode=2001)
        after = make_media(item_id="new-id", path="/medias/movies/new.mkv", inode=2001)
        diff = diff_snapshots(snapshot(media=[before]), snapshot(media=[after]))
        self.assertEqual(diff.lost_media, [])
        self.assertEqual(diff.new_media_count, 1)

    def test_same_path_protected_without_inode(self):
        # inode 为 0（文件不可见/stat 失败）时，只要路径还在就不算丢失
        before = make_media(item_id="old-id", path="/medias/movies/a.mkv", inode=0, dev=0)
        after = make_media(item_id="new-id", path="/medias/movies/a.mkv", inode=0, dev=0)
        diff = diff_snapshots(snapshot(media=[before]), snapshot(media=[after]))
        self.assertEqual(diff.lost_media, [])
        self.assertEqual(diff.new_media_count, 1)

    def test_normalized_path_protected(self):
        # 历史快照可能带 "./" 等写法，规范化后同路径也不应误判丢失
        before = make_media(item_id="old", path="/medias/movies/./a.mkv", inode=0, dev=0)
        after = make_media(item_id="new", path="/medias/movies/a.mkv", inode=0, dev=0)
        diff = diff_snapshots(snapshot(media=[before]), snapshot(media=[after]))
        self.assertEqual(diff.lost_media, [])

    def test_same_key_never_lost(self):
        item = make_media()
        diff = diff_snapshots(snapshot(media=[item]), snapshot(media=[item]))
        self.assertEqual(diff.lost_media, [])
        self.assertEqual(diff.new_media_count, 0)

    def test_lost_torrents_and_new_counts(self):
        t1 = make_torrent("h1")
        t2 = make_torrent("h2")
        t3 = make_torrent("h3")
        previous = snapshot(torrents=[t1, t2])
        current = snapshot(torrents=[t2, t3])
        diff = diff_snapshots(previous, current)
        self.assertEqual([item.key for item in diff.lost_torrents], [t1.key])
        self.assertEqual(diff.new_torrent_count, 1)
        self.assertFalse(diff.lost_media)

    def test_no_changes(self):
        item = make_media()
        torrent = make_torrent()
        diff = diff_snapshots(
            snapshot(media=[item], torrents=[torrent]),
            snapshot(media=[item], torrents=[torrent]),
        )
        self.assertTrue(diff.is_empty)

    def test_diff_to_dict(self):
        lost = make_media()
        diff = SnapshotDiff(lost_media=[lost], lost_torrents=[make_torrent()], new_media_count=2)
        data = diff.to_dict()
        self.assertEqual(data["new_media_count"], 2)
        self.assertEqual(data["lost_media"][0]["server"], lost.server)
        restored = SnapshotDiff.from_dict(data)
        self.assertEqual(restored.lost_media[0].key, lost.key)
        self.assertEqual(restored.lost_torrents[0].key, "主QB|h1")


class PersistedStateTest(unittest.TestCase):
    def test_roundtrip(self):
        state = PersistedState()
        media = make_media(server="Jellyfin", item_id="abc", path="/medias/a.mkv", inode=5, dev=9)
        torrent = make_torrent("deadbeef")
        state.snapshot = snapshot(media=[media], torrents=[torrent])
        state.links.add_media_inode(media.inode_key, media)
        state.links.add_torrent_inode(media.inode_key, torrent)
        state.links.add_link(media, torrent, evidence="inode")
        state.pending = [
            PendingAction(
                action_id="torrents-abc",
                kind="delete_torrents",
                created_at=1.0,
                due_at=2.0,
                reason="测试",
                payload={"torrents": [{"downloader": "主QB", "hash": "deadbeef"}]},
                dry_run=True,
            )
        ]
        state.last_poll_at = 123.5
        state.last_scan_summary = {"media_total": 3, "skipped": {"downloading": 1}}
        state.last_error = "警告"

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.save(state)
            self.assertTrue((pathlib.Path(tmp) / STATE_FILENAME).is_file())
            loaded = store.load()

        self.assertEqual(loaded.last_poll_at, 123.5)
        self.assertEqual(loaded.last_error, "警告")
        self.assertEqual(loaded.last_scan_summary["media_total"], 3)
        self.assertEqual(loaded.version, DATA_VERSION)
        self.assertEqual(list(loaded.snapshot.media.keys()), [media.key])
        self.assertEqual(loaded.snapshot.media[media.key].inode, 5)
        self.assertEqual(loaded.links.link_count, 1)
        self.assertEqual(loaded.links.evidence[f"{media.key}->{torrent.key}"], "inode")
        self.assertEqual(len(loaded.pending), 1)
        self.assertEqual(loaded.pending[0].action_id, "torrents-abc")
        self.assertTrue(loaded.pending[0].dry_run)

    def test_missing_file_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            state = store.load()
        self.assertIsInstance(state, PersistedState)
        self.assertEqual(state.snapshot.media, {})
        self.assertEqual(state.pending, [])
        self.assertEqual(state.last_poll_at, 0.0)
        self.assertEqual(store.last_error, "")

    def test_corrupt_file_returns_empty_state_without_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / STATE_FILENAME
            path.write_bytes(b"this is not gzip")
            store = StateStore(tmp)
            state = store.load()
            self.assertEqual(state.pending, [])
            self.assertTrue(store.last_error)
            # 再写坏 JSON 的 gzip 文件
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write("{not a json")
            state = store.load()
            self.assertEqual(state.pending, [])

    def test_non_object_json_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / STATE_FILENAME
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump([1, 2, 3], handle)
            state = StateStore(tmp).load()
            self.assertEqual(state.snapshot.media, {})

    def test_version_mismatch_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / STATE_FILENAME
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump({"version": DATA_VERSION + 1, "snapshot": {}, "pending": []}, handle)
            state = StateStore(tmp).load()
            self.assertEqual(state.pending, [])
            self.assertEqual(state.version, DATA_VERSION)

    def test_save_is_atomic_and_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            state = PersistedState(last_error="first")
            store.save(state)
            state.last_error = "second"
            store.save(state)
            files = sorted(path.name for path in pathlib.Path(tmp).iterdir())
            self.assertEqual(files, [STATE_FILENAME])
            self.assertEqual(store.load().last_error, "second")

    def test_save_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "blocker"
            blocker.write_text("i am a file", encoding="utf-8")
            store = StateStore(blocker)  # data_dir 实际是一个文件 -> mkdir 失败
            store.save(PersistedState())  # 不应抛异常
            self.assertTrue(store.last_error)
            # clear 同样不应抛异常
            store.clear()

    def test_clear_removes_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.save(PersistedState(last_error="x"))
            self.assertTrue(store.exists)
            store.clear()
            self.assertFalse(store.exists)
            self.assertEqual(store.load().last_error, "")
            # 重复 clear 不报错
            store.clear()

    def test_from_dict_tolerates_garbage(self):
        state = PersistedState.from_dict(
            {
                "version": "bad",
                "snapshot": "bad",
                "links": None,
                "pending": [1, "x", {"action_id": "a1"}, None],
                "last_poll_at": "nan-ish",
                "last_scan_summary": ["bad"],
                "last_error": 123,
            }
        )
        self.assertEqual(len(state.pending), 1)
        self.assertEqual(state.pending[0].action_id, "a1")
        self.assertEqual(state.last_poll_at, 0.0)
        self.assertEqual(state.last_scan_summary, {})
        self.assertEqual(state.last_error, "123")
        self.assertEqual(PersistedState.from_dict(None).version, DATA_VERSION)

    def test_state_dir_property(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(pathlib.Path(tmp))
            self.assertEqual(store.path.name, STATE_FILENAME)
            self.assertEqual(store.data_dir, pathlib.Path(tmp))

    def test_pending_list_not_truncated_by_store(self):
        actions = [
            PendingAction(
                action_id=f"a{i}", kind="delete_torrents", created_at=0, due_at=0, reason="r"
            )
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.save(PersistedState(pending=actions))
            self.assertEqual(len(store.load().pending), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
