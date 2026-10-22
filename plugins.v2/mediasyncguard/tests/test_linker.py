"""MediaSyncGuard ``linker.py`` 单元测试（仅标准库 + 真实临时文件/硬链接）。

覆盖：文件系统遍历（单文件/目录/符号链接/深度限制/缓存/错误），
LinkBuilder 的 inode 关联、cross-seed 共享、history 兜底与统计信息。

运行：``python3 plugins.v2/mediasyncguard/tests/test_linker.py``
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from models import MediaItem, TorrentItem, TorrentRef  # noqa: E402
from linker import (  # noqa: E402
    FileSystemWalker,
    InodeInfo,
    LinkBuilder,
    LinkBuildResult,
    LinkBuildStats,
)


class TempDirTestCase(unittest.TestCase):
    """所有用例都在临时目录内创建真实文件，不触碰真实媒体库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def make_file(self, relative: str, content: bytes = b"data", root: pathlib.Path = None) -> pathlib.Path:
        path = (root or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def make_hardlink(self, source: pathlib.Path, relative: str, root: pathlib.Path = None) -> pathlib.Path:
        target = (root or self.root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        return target

    def inode_of(self, path: pathlib.Path):
        st = os.stat(path)
        return int(st.st_dev), int(st.st_ino)

    def make_media(self, path: pathlib.Path, item_id: str = "m1", inode: int = 0, dev: int = 0) -> MediaItem:
        return MediaItem(server="Jellyfin", item_id=item_id, path=str(path), inode=inode, dev=dev)


class FileSystemWalkerTest(TempDirTestCase):
    def test_stat_returns_inode_info(self):
        path = self.make_file("a.mkv", b"hello")
        info = FileSystemWalker().stat(str(path))
        self.assertIsInstance(info, InodeInfo)
        st = os.stat(path)
        self.assertEqual((info.dev, info.inode), (st.st_dev, st.st_ino))
        self.assertEqual(info.size, 5)
        self.assertAlmostEqual(info.mtime, st.st_mtime, places=3)
        self.assertEqual(info.path, str(path))

    def test_stat_missing_returns_none(self):
        walker = FileSystemWalker()
        self.assertIsNone(walker.stat(str(self.root / "missing.mkv")))
        self.assertIsNone(walker.stat(""))
        self.assertEqual(walker.walk_errors, 0)

    def test_collect_single_video_file(self):
        path = self.make_file("single.mkv")
        inodes = FileSystemWalker().collect_video_inodes(str(path))
        self.assertEqual(inodes, [self.inode_of(path)])

    def test_collect_single_non_video_file_still_returns_inode(self):
        # 契约：path 为文件时直接 stat（单文件种子的 content_path 就是内容文件）
        path = self.make_file("book.epub")
        inodes = FileSystemWalker().collect_video_inodes(str(path))
        self.assertEqual(inodes, [self.inode_of(path)])

    def test_collect_directory_videos_only(self):
        video1 = self.make_file("show/e01.mkv")
        video2 = self.make_file("show/sub/e02.mp4")
        self.make_file("show/e01.srt")
        self.make_file("show/readme.txt")
        inodes = FileSystemWalker().collect_video_inodes(str(self.root / "show"))
        self.assertEqual(len(inodes), 2)
        self.assertIn(self.inode_of(video1), inodes)
        self.assertIn(self.inode_of(video2), inodes)

    def test_collect_directory_does_not_follow_directory_symlink_loop(self):
        video = self.make_file("show/e01.mkv")
        (self.root / "show" / "loop").symlink_to(self.root / "show", target_is_directory=True)
        inodes = FileSystemWalker().collect_video_inodes(str(self.root / "show"))
        self.assertEqual(inodes, [self.inode_of(video)])

    def test_collect_directory_skips_external_directory_symlink(self):
        outside = self.make_file("outside/e99.mkv")
        show = self.root / "show"
        show.mkdir()
        (show / "linked-outside").symlink_to(outside.parent, target_is_directory=True)
        inodes = FileSystemWalker().collect_video_inodes(str(show))
        self.assertEqual(inodes, [])

    def test_collect_follows_file_symlink(self):
        real = self.make_file("real/actual.mkv")
        show = self.root / "show"
        show.mkdir()
        (show / "alias.mkv").symlink_to(real)
        inodes = FileSystemWalker().collect_video_inodes(str(show))
        self.assertEqual(inodes, [self.inode_of(real)])

    def test_collect_respects_max_depth(self):
        root = self.root / "walk"
        self.make_file("walk/a.mkv", b"a")
        self.make_file("walk/sub/b.mkv", b"b")
        self.make_file("walk/sub/deep/c.mkv", b"c")
        all_inodes = FileSystemWalker().collect_video_inodes(str(root))
        self.assertEqual(len(all_inodes), 3)
        shallow = FileSystemWalker(max_depth=1).collect_video_inodes(str(root))
        self.assertEqual(len(shallow), 2)
        self.assertEqual(
            FileSystemWalker(max_depth=0).collect_video_inodes(str(root)),
            [self.inode_of(root / "a.mkv")],
        )

    def test_collect_missing_path_returns_empty(self):
        walker = FileSystemWalker()
        self.assertEqual(walker.collect_video_inodes(str(self.root / "missing")), [])
        self.assertEqual(walker.collect_video_inodes(""), [])
        self.assertGreaterEqual(walker.walk_errors, 1)

    def test_collect_broken_symlink_counts_walk_error(self):
        show = self.root / "show"
        show.mkdir()
        (show / "broken.mkv").symlink_to(self.root / "nope.mkv")
        video = self.make_file("show/ok.mkv")
        walker = FileSystemWalker()
        inodes = walker.collect_video_inodes(str(show))
        self.assertEqual(inodes, [self.inode_of(video)])
        self.assertEqual(walker.walk_errors, 1)

    def test_collect_caches_by_realpath(self):
        video = self.make_file("show/e01.mkv")
        alias = self.root / "alias"
        alias.symlink_to(self.root / "show", target_is_directory=True)
        walker = FileSystemWalker()
        first = walker.collect_video_inodes(str(self.root / "show"))
        second = walker.collect_video_inodes(str(alias))
        self.assertEqual(first, second)
        self.assertEqual(first, [self.inode_of(video)])
        self.assertEqual(walker.cache_size, 1)
        walker.reset_cache()
        self.assertEqual(walker.cache_size, 0)
        self.assertEqual(walker.collect_video_inodes(str(alias)), first)

    def test_collect_max_files_cap(self):
        for index in range(5):
            self.make_file(f"many/e{index}.mkv", str(index).encode())
        walker = FileSystemWalker(max_files_per_torrent=2)
        inodes = walker.collect_video_inodes(str(self.root / "many"))
        self.assertLessEqual(len(inodes), 2)
        self.assertGreaterEqual(len(inodes), 1)

    def test_collect_empty_directory(self):
        (self.root / "empty").mkdir()
        self.assertEqual(FileSystemWalker().collect_video_inodes(str(self.root / "empty")), [])


class LinkBuildStatsTest(unittest.TestCase):
    def test_to_dict_uses_contract_keys(self):
        stats = LinkBuildStats(
            media_total=3,
            media_with_inode=2,
            torrents_total=4,
            torrents_walked=1,
            torrents_unsupported=2,
            torrents_missing_path=1,
            video_files=9,
            walk_errors=5,
            linked_pairs=7,
            history_linked_pairs=6,
        )
        self.assertEqual(
            stats.to_dict(),
            {
                "media_total": 3,
                "torrent_total": 4,
                "link_pairs": 7,
                "torrents_walked": 1,
                "torrents_unsupported": 2,
                "torrents_missing_path": 1,
                "video_files": 9,
                "walk_errors": 5,
            },
        )


class LinkBuilderTest(TempDirTestCase):
    def build(self, media, torrents, history_rows=None):
        return LinkBuilder().build(media, torrents, history_rows)

    def test_single_file_torrent_links_by_inode(self):
        source = self.make_file("media/movie.mkv", b"movie")
        download_file = self.make_hardlink(source, "downloads/movie.mkv")
        media = self.make_media(source, "m1")  # inode=0 -> builder 自动 stat 补全
        torrent = TorrentItem(
            downloader="主QB", hash="hash1", content_path=str(download_file), path_exists=True
        )
        result = self.build([media], [torrent])
        self.assertIsInstance(result, LinkBuildResult)
        self.assertTrue(media.has_inode)
        self.assertEqual(media.inode_key, self.inode_of(source))
        self.assertEqual(media.size, 5)
        stats = result.stats
        self.assertEqual(stats.media_total, 1)
        self.assertEqual(stats.media_with_inode, 1)
        self.assertEqual(stats.torrents_total, 1)
        self.assertEqual(stats.torrents_walked, 1)
        self.assertEqual(stats.video_files, 1)
        self.assertEqual(stats.linked_pairs, 1)
        self.assertEqual(stats.history_linked_pairs, 0)
        self.assertEqual(stats.walk_errors, 0)
        self.assertEqual(result.index.media_of(torrent), [media_ref(media)])
        self.assertEqual(result.index.torrents_of(media), [TorrentRef("主QB", "hash1")])
        self.assertEqual(torrent.video_files, 1)
        self.assertEqual(result.index.link_count, 1)

    def test_directory_torrent_links_all_videos(self):
        source1 = self.make_file("media/ep1.mkv", b"ep1")
        source2 = self.make_file("media/ep2.mkv", b"ep2")
        show = self.root / "downloads" / "show"
        self.make_hardlink(source1, "downloads/show/ep1.mkv")
        self.make_hardlink(source2, "downloads/show/ep2.mkv")
        self.make_file("downloads/show/ep1.srt")
        media1 = self.make_media(source1, "m1")
        media2 = self.make_media(source2, "m2")
        unlinked = self.make_media(self.make_file("media/other.mkv"), "m3")
        torrent = TorrentItem(downloader="主QB", hash="abc", content_path=str(show))
        result = self.build([media1, media2, unlinked], [torrent])
        self.assertEqual(result.stats.linked_pairs, 2)
        self.assertEqual(result.stats.video_files, 2)
        self.assertEqual(result.stats.torrents_walked, 1)
        self.assertEqual(len(result.index.media_of(torrent)), 2)
        self.assertEqual(result.index.torrents_of(unlinked), [])
        self.assertEqual(len(result.index.inodes_of_torrent(torrent)), 2)

    def test_cross_seed_shared_directory_uses_cache_and_links_both(self):
        source = self.make_file("media/ep1.mkv", b"ep1")
        self.make_hardlink(source, "downloads/show/ep1.mkv")
        alias = self.root / "downloads" / "show-alias"
        alias.symlink_to(self.root / "downloads" / "show", target_is_directory=True)
        media = self.make_media(source, "m1")
        torrent_a = TorrentItem(downloader="主QB", hash="aaa", content_path=str(self.root / "downloads" / "show"))
        torrent_b = TorrentItem(downloader="主QB", hash="bbb", content_path=str(alias))
        builder = LinkBuilder()
        result = builder.build([media], [torrent_a, torrent_b])
        self.assertEqual(builder.walker.cache_size, 1)
        self.assertEqual(result.stats.linked_pairs, 2)
        self.assertEqual(len(result.index.current_torrents_of_inode(media.inode_key)), 2)
        self.assertEqual(
            {ref.hash for ref in result.index.current_torrents_of_inode(media.inode_key)},
            {"aaa", "bbb"},
        )
        # 两个种子都登记了同一 inode，供 guard 检测共享
        self.assertEqual(len(result.index.inodes_of_torrent(torrent_a)), 1)
        self.assertEqual(result.index.inodes_of_torrent(torrent_a), result.index.inodes_of_torrent(torrent_b))

    def test_cross_seed_hardlinks_share_inode(self):
        source = self.make_file("media/ep1.mkv", b"ep1")
        self.make_hardlink(source, "downloads/a/ep1.mkv")
        self.make_hardlink(source, "downloads/b/ep1.mkv")
        media = self.make_media(source, "m1")
        torrent_a = TorrentItem(downloader="主QB", hash="aaa", content_path=str(self.root / "downloads" / "a"))
        torrent_b = TorrentItem(downloader="主QB", hash="bbb", content_path=str(self.root / "downloads" / "b"))
        result = self.build([media], [torrent_a, torrent_b])
        self.assertEqual(result.stats.linked_pairs, 2)
        self.assertEqual(len(result.index.current_torrents_of_inode(media.inode_key)), 2)

    def test_multiple_media_same_inode_both_linked(self):
        source = self.make_file("media/movie.mkv", b"movie")
        self.make_hardlink(source, "downloads/movie.mkv")
        media1 = self.make_media(source, "m1")
        media2 = self.make_media(source, "m2")
        torrent = TorrentItem(
            downloader="主QB", hash="h", content_path=str(self.root / "downloads" / "movie.mkv")
        )
        result = self.build([media1, media2], [torrent])
        self.assertEqual(result.stats.linked_pairs, 2)
        self.assertEqual(len(result.index.media_of(torrent)), 2)
        self.assertEqual(len(result.index.torrents_of(media1)), 1)

    def test_history_fallback_links_by_dest_path(self):
        media_file = self.make_file("media/movie.mkv", b"movie")
        other = self.make_file("downloads/other.mkv", b"other")
        media = self.make_media(media_file, "m1")
        torrent = TorrentItem(downloader="主QB", hash="AbC123", content_path=str(other))
        # 带 "./" 的路径应被 normpath 后匹配
        history_path = f"{media_file.parent}/./{media_file.name}"
        result = self.build(
            [media], [torrent], history_rows=[("主QB", "abc123", history_path)]
        )
        self.assertEqual(result.stats.linked_pairs, 0)
        self.assertEqual(result.stats.history_linked_pairs, 1)
        self.assertEqual(result.index.media_of(torrent), [media_ref(media)])
        pair_key = f"{media.key}->{torrent.key}"
        self.assertEqual(result.index.evidence[pair_key], "history")

    def test_history_rows_accepts_generator(self):
        media_file = self.make_file("media/movie.mkv", b"movie")
        other = self.make_file("downloads/other.mkv", b"other")
        media = self.make_media(media_file, "m1")
        torrent = TorrentItem(downloader="主QB", hash="gen1", content_path=str(other))
        rows = (row for row in [("主QB", "gen1", str(media_file))])
        result = self.build([media], [torrent], history_rows=rows)
        self.assertEqual(result.stats.history_linked_pairs, 1)

    def test_history_fallback_skips_unknown_torrent_or_missing_media(self):
        media_file = self.make_file("media/movie.mkv", b"movie")
        media = self.make_media(media_file, "m1")
        torrent = TorrentItem(downloader="主QB", hash="known", content_path=str(self.root / "downloads"))
        result = self.build(
            [media],
            [torrent],
            history_rows=[
                ("主QB", "unknown-hash", str(media_file)),  # 种子 key 不存在
                ("主QB", "known", "/medias/not-in-library.mkv"),  # 媒体不存在
                ("", "", ""),  # 空行
            ],
        )
        self.assertEqual(result.stats.history_linked_pairs, 0)
        self.assertEqual(result.index.media_of(torrent), [])

    def test_inode_evidence_has_priority_over_history(self):
        source = self.make_file("media/movie.mkv", b"movie")
        self.make_hardlink(source, "downloads/movie.mkv")
        media = self.make_media(source, "m1")
        torrent = TorrentItem(
            downloader="主QB", hash="h", content_path=str(self.root / "downloads" / "movie.mkv")
        )
        result = self.build(
            [media], [torrent], history_rows=[("主QB", "h", str(source))]
        )
        pair_key = f"{media.key}->{torrent.key}"
        self.assertEqual(result.index.evidence[pair_key], "inode")
        self.assertEqual(result.stats.linked_pairs, 1)
        self.assertEqual(result.stats.history_linked_pairs, 0)

    def test_stats_for_unsupported_and_missing_content(self):
        walk_dir = self.root / "downloads" / "one"
        self.make_file("downloads/one/e1.mkv")
        walked = TorrentItem(downloader="主QB", hash="walked", content_path=str(walk_dir))
        unsupported = TorrentItem(
            downloader="副QB", hash="unsupported", content_path="/invisible/path", unsupported_path=True
        )
        missing = TorrentItem(downloader="主QB", hash="missing", content_path="")
        result = self.build([], [walked, unsupported, missing])
        stats = result.stats
        self.assertEqual(stats.torrents_total, 3)
        self.assertEqual(stats.torrents_walked, 1)
        self.assertEqual(stats.torrents_unsupported, 1)
        self.assertEqual(stats.torrents_missing_path, 1)
        self.assertEqual(stats.video_files, 1)
        self.assertEqual(walked.video_files, 1)
        self.assertEqual(unsupported.video_files, 0)
        self.assertEqual(result.index.link_count, 0)

    def test_unreadable_media_path_is_tolerated(self):
        missing = MediaItem(server="Jellyfin", item_id="m1", path=str(self.root / "gone.mkv"))
        torrent = TorrentItem(downloader="主QB", hash="h", content_path=str(self.root / "gone-dir"))
        result = self.build([missing], [torrent])
        self.assertFalse(missing.has_inode)
        self.assertEqual(result.stats.media_with_inode, 0)
        self.assertEqual(result.stats.walk_errors, 1)  # 种子内容路径不存在
        self.assertEqual(result.index.link_count, 0)

    def test_media_without_path_is_skipped_safely(self):
        orphan = MediaItem(server="Jellyfin", item_id="m1", path="")
        result = self.build([orphan], [])
        self.assertEqual(result.stats.media_total, 1)
        self.assertEqual(result.stats.media_with_inode, 0)

    def test_empty_inputs(self):
        result = self.build(None, None)
        self.assertEqual(result.index.link_count, 0)
        self.assertEqual(result.stats.media_total, 0)
        self.assertEqual(result.stats.torrents_total, 0)

    def test_builder_reuses_injected_walker(self):
        walker = FileSystemWalker()
        builder = LinkBuilder(walker)
        self.assertIs(builder.walker, walker)
        self.assertIs(LinkBuilder().walker.__class__, FileSystemWalker)


def media_ref(media: MediaItem):
    from models import MediaRef

    return MediaRef(server=media.server, item_id=media.item_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
