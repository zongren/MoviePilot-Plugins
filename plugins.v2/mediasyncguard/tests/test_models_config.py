"""MediaSyncGuard 纯数据模型与配置测试（不需要 MoviePilot 环境）。

运行：``python3 plugins.v2/mediasyncguard/tests/test_models_config.py``
"""

from __future__ import annotations

import pathlib
import sys
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from config import PluginConfig, map_path, parse_path_mappings  # noqa: E402
from models import (  # noqa: E402
    LinkIndex,
    MediaItem,
    PendingAction,
    Snapshot,
    TorrentItem,
    inode_key_str,
    is_sidecar_file,
    is_video_file,
    media_key,
    media_key as make_media_key,
    parse_inode_key,
    torrent_key,
)


class PathMappingTest(unittest.TestCase):
    def test_map_path_respects_segment_boundaries(self):
        mappings = parse_path_mappings(["/media:/medias"])
        self.assertEqual(map_path("/media/TV/a.mkv", mappings), "/medias/TV/a.mkv")
        self.assertEqual(map_path("/media", mappings), "/medias")
        # /media2 不能被 /media 前缀误伤
        self.assertEqual(map_path("/media2/TV/a.mkv", mappings), "/media2/TV/a.mkv")

    def test_parse_path_mappings_ignores_invalid_lines(self):
        self.assertEqual(parse_path_mappings("bad line\n/media:/medias\n\n"), [("/media", "/medias")])
        self.assertEqual(parse_path_mappings(None), [])


class PluginConfigTest(unittest.TestCase):
    def test_defaults_are_safe(self):
        config = PluginConfig.from_dict({})
        self.assertFalse(config.enabled)
        self.assertTrue(config.dry_run)
        self.assertFalse(config.auto_media_to_torrent)
        self.assertFalse(config.auto_torrent_to_media)
        self.assertFalse(config.auto_enabled)
        self.assertEqual(config.effective_poll_seconds, 60)
        self.assertEqual(config.ignore_categories, ["KEEP"])
        self.assertEqual(config.media_path_mappings, [("/media", "/medias")])

    def test_string_values_are_coerced(self):
        config = PluginConfig.from_dict(
            {
                "enabled": "True",
                "dry_run": "false",
                "poll_seconds": "30",
                "grace_seconds": "0",
                "downloaders": "主QB, 副QB",
                "ignore_tags": ["a", "b"],
                "batch_limit_torrents": "5",
                "auto_torrent_to_media": 1,
            }
        )
        self.assertTrue(config.enabled)
        self.assertFalse(config.dry_run)
        self.assertEqual(config.effective_poll_seconds, 30)
        self.assertEqual(config.grace_seconds, 0)
        self.assertEqual(config.downloaders, ["主QB", "副QB"])
        self.assertEqual(config.ignore_tags, ["a", "b"])
        self.assertEqual(config.batch_limit_torrents, 5)
        self.assertTrue(config.auto_torrent_to_media)

    def test_poll_interval_clamped(self):
        self.assertEqual(PluginConfig.from_dict({"poll_seconds": 1}).effective_poll_seconds, 15)
        self.assertEqual(PluginConfig.from_dict({"poll_seconds": 999999}).effective_poll_seconds, 3600)

    def test_roundtrip_and_merged(self):
        config = PluginConfig.from_dict(
            {
                "enabled": True,
                "downloaders": ["主QB"],
                "media_servers": ["Jellyfin"],
                "media_path_map": ["/media:/medias"],
            }
        )
        data = config.to_dict()
        again = PluginConfig.from_dict(data)
        self.assertEqual(again.to_dict(), data)
        merged = config.merged(dry_run=False)
        self.assertFalse(merged.dry_run)
        self.assertTrue(merged.enabled)
        self.assertEqual(merged.downloaders, ["主QB"])

    def test_empty_lists_are_preserved(self):
        config = PluginConfig.from_dict({"downloaders": [], "media_servers": []})
        self.assertEqual(config.downloaders, [])
        self.assertEqual(config.media_servers, [])


class ModelsTest(unittest.TestCase):
    def test_file_extensions(self):
        self.assertTrue(is_video_file("/x/a.MKV"))
        self.assertFalse(is_video_file("/x/a.srt"))
        self.assertTrue(is_sidecar_file("/x/a.nfo"))
        self.assertTrue(is_sidecar_file("/x/a.zh.srt"))
        self.assertFalse(is_sidecar_file("/x/a.mkv"))

    def test_downloader_incomplete_suffix_is_still_video(self):
        self.assertTrue(is_video_file("/x/movie.mkv.!qB"))
        self.assertTrue(is_video_file("/x/movie.mkv.!qB"))
        self.assertTrue(is_video_file("/x/movie.mkv.!ut"))
        self.assertTrue(is_video_file("/x/movie.mp4.part"))
        self.assertFalse(is_video_file("/x/readme.txt.!qB"))
        self.assertTrue(is_sidecar_file("/x/movie.srt.!qB"))

    def test_media_item_display_and_serialization(self):
        item = MediaItem(
            server="Jellyfin",
            item_id="abc",
            path="/medias/TV/老友记 (1994)/Season 1/老友记 - S01E02.mkv",
            item_type="Episode",
            series_name="老友记",
            season=1,
            episode=2,
        )
        self.assertEqual(item.key, media_key("Jellyfin", "abc"))
        self.assertEqual(item.display_title, "老友记 S01 E02")
        self.assertEqual(item.folder, "/medias/TV/老友记 (1994)/Season 1")
        again = MediaItem.from_dict(item.to_dict())
        self.assertEqual(again.to_dict(), item.to_dict())

    def test_media_item_extra_inode_keys_roundtrip(self):
        item = MediaItem(server="Jellyfin", item_id="x", path="/m", inode=1, dev=2)
        item.extra_inode_keys = [(2, 100), (2, 200)]
        restored = MediaItem.from_dict(item.to_dict())
        self.assertEqual(restored.extra_inode_keys, [(2, 100), (2, 200)])
        restored2 = MediaItem.from_dict(
            {"server": "J", "item_id": "y", "extra_inode_keys": [["3", "4"], ["bad"], []]}
        )
        self.assertEqual(restored2.extra_inode_keys, [(3, 4)])

    def test_torrent_item_serialization_and_completed(self):
        torrent = TorrentItem(
            downloader="主QB",
            hash="ABC123",
            name="x",
            tags=["a", "b"],
            progress=1.0,
        )
        self.assertEqual(torrent.key, torrent_key("主QB", "abc123"))
        self.assertTrue(torrent.completed)
        self.assertFalse(TorrentItem(downloader="主QB", hash="x", progress=0.5).completed)
        again = TorrentItem.from_dict(torrent.to_dict())
        self.assertEqual(again.hash, "abc123")
        self.assertEqual(again.tags, ["a", "b"])

    def test_snapshot_roundtrip(self):
        media = MediaItem(server="S", item_id="1", path="/medias/a.mkv", inode=5, dev=1)
        torrent = TorrentItem(downloader="D", hash="h", name="n")
        snapshot = Snapshot(media={media.key: media}, torrents={torrent.key: torrent})
        again = Snapshot.from_dict(snapshot.to_dict())
        self.assertEqual(again.media[media.key].path, "/medias/a.mkv")
        self.assertEqual(again.torrents[torrent.key].hash, "h")
        self.assertEqual(again.media_inode_keys, {(1, 5)})

    def test_link_index_roundtrip_and_inode_helpers(self):
        media = MediaItem(server="S", item_id="1", path="/medias/a.mkv", inode=5, dev=1)
        torrent = TorrentItem(downloader="D", hash="h", name="n")
        index = LinkIndex()
        index.add_media_inode(media.inode_key, media)
        index.add_torrent_inode(media.inode_key, torrent)
        index.add_link(media, torrent, "inode")
        self.assertEqual(index.media_of(torrent), index.torrent_media[torrent.key])
        self.assertEqual(len(index.torrents_of(media)), 1)
        self.assertEqual(index.evidence[f"{media.key}->{torrent.key}"], "inode")
        self.assertEqual(index.current_torrents_of_inode(media.inode_key), index.inode_torrents["1:5"])
        self.assertEqual(index.inodes_of_torrent(torrent), ["1:5"])
        again = LinkIndex.from_dict(index.to_dict())
        self.assertEqual(again.to_dict(), index.to_dict())

    def test_inode_helpers(self):
        self.assertEqual(inode_key_str((58, 123)), "58:123")
        self.assertEqual(parse_inode_key("58:123"), (58, 123))
        self.assertEqual(parse_inode_key("bad"), (0, 0))

    def test_pending_action_roundtrip(self):
        action = PendingAction(
            action_id="a1",
            kind="delete_torrents",
            created_at=1.0,
            due_at=2.0,
            reason="test",
            payload={"torrents": []},
            dry_run=True,
        )
        again = PendingAction.from_dict(action.to_dict())
        self.assertEqual(again.to_dict(), action.to_dict())
        self.assertTrue(again.is_pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
