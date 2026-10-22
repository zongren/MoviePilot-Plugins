"""MediaSyncGuard 文件清理模块（cleanup.py）单元测试。

运行：``python3 plugins.v2/mediasyncguard/tests/test_cleanup.py``
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

from cleanup import (  # noqa: E402
    apply_media_cleanup,
    is_path_under,
    is_safe_delete_target,
    plan_media_cleanup,
)
from config import PluginConfig  # noqa: E402
from models import MediaItem  # noqa: E402


def _touch(path: pathlib.Path, content: bytes = b"x") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class CleanupTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-cleanup-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.config = PluginConfig(
            media_delete_roots=[str(self.root)],
            download_delete_roots=[str(self.downloads)],
            delete_media_sidecars=True,
            cleanup_empty_dirs=True,
        )

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _media(path: pathlib.Path, item_type: str = "Movie", name: str = "") -> MediaItem:
        return MediaItem(
            server="Jellyfin",
            item_id="abc",
            path=str(path),
            name=name or path.stem,
            item_type=item_type,
        )


class PathSafetyTest(CleanupTestBase):
    def test_is_path_under(self):
        self.assertTrue(is_path_under(str(self.root / "a" / "b.mkv"), [str(self.root)]))
        self.assertTrue(is_path_under(str(self.root / "a"), [str(self.root)]))
        # 目录本身也算“在目录下”
        self.assertTrue(is_path_under(str(self.root), [str(self.root)]))
        self.assertFalse(is_path_under(str(self.root) + "-other/x.mkv", [str(self.root)]))
        self.assertFalse(is_path_under("", [str(self.root)]))
        self.assertFalse(is_path_under(str(self.root), []))
        # 带尾斜杠/未规范化也能匹配
        self.assertTrue(is_path_under(str(self.root / "a"), [str(self.root) + "/"]))

    def test_safe_delete_target(self):
        self.assertTrue(is_safe_delete_target(str(self.root / "Movies/a.mkv"), self.config))
        # 下载器目录内的文件不允许在文件清理阶段删除
        self.assertFalse(
            is_safe_delete_target(str(self.downloads / "a.mkv"), self.config)
        )
        # 根目录本身、以及根目录的父目录都不允许
        self.assertFalse(is_safe_delete_target(str(self.root), self.config))
        self.assertFalse(is_safe_delete_target(str(self.root.parent), self.config))
        self.assertFalse(is_safe_delete_target("", self.config))
        # 显式传入 media_roots 时优先生效
        other = self.root / "Other"
        other.mkdir()
        self.assertTrue(
            is_safe_delete_target(
                str(other / "a.mkv"), self.config, media_roots=[str(other)]
            )
        )


class PlanMediaCleanupTest(CleanupTestBase):
    def test_movie_plan_sidecars_and_empty_dir(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        sidecars = [
            _touch(folder / "Movie (2020).srt"),
            _touch(folder / "Movie (2020).nfo"),
            _touch(folder / "poster.jpg"),
        ]
        plan = plan_media_cleanup(self._media(movie), self.config)
        self.assertEqual(sorted(plan["sidecar_files"]), sorted(str(p) for p in sidecars))
        self.assertEqual(plan["remove_dirs"], [str(folder)])
        self.assertEqual(plan["skipped"], [])

    def test_movie_with_other_video_keeps_dir(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        _touch(folder / "Movie (2020).srt")
        _touch(folder / "Movie (2020)-trailer.mkv")
        plan = plan_media_cleanup(self._media(movie), self.config)
        self.assertEqual(plan["remove_dirs"], [])
        self.assertEqual(len(plan["sidecar_files"]), 1)

    def test_movie_with_other_version_keeps_folder_level_artwork(self):
        """同目录存在其它版本视频时，只删同名附属文件，保留共用海报/NFO。"""
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020) - 1080p.mkv")
        _touch(folder / "Movie (2020) - 2160p.mkv")
        same_stem = _touch(folder / "Movie (2020) - 1080p.srt")
        shared_sidecars = [
            _touch(folder / "poster.jpg"),
            _touch(folder / "movie.nfo"),
            _touch(folder / "fanart.jpg"),
        ]
        plan = plan_media_cleanup(self._media(movie), self.config)
        self.assertEqual(plan["sidecar_files"], [str(same_stem)])
        for shared in shared_sidecars:
            self.assertNotIn(str(shared), plan["sidecar_files"])
        self.assertEqual(plan["remove_dirs"], [])

    def test_movie_with_unknown_leftover_keeps_dir(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        _touch(folder / "Movie (2020).srt")
        _touch(folder / "readme.unknownext")
        plan = plan_media_cleanup(self._media(movie), self.config)
        self.assertEqual(plan["remove_dirs"], [])

    def test_episode_plan_only_same_stem_sidecars(self):
        folder = self.root / "TV" / "Show" / "Season 1"
        episode = _touch(folder / "Show - S01E01.mkv")
        same = _touch(folder / "Show - S01E01.srt")
        _touch(folder / "Show - S01E01.ass")
        _touch(folder / "Show - S01E02.srt")
        _touch(folder / "Show - S01E01.nfo")
        plan = plan_media_cleanup(self._media(episode, item_type="Episode"), self.config)
        self.assertEqual(
            sorted(plan["sidecar_files"]),
            sorted(
                [
                    str(same),
                    str(folder / "Show - S01E01.ass"),
                    str(folder / "Show - S01E01.nfo"),
                ]
            ),
        )
        # 剧集永远不清理目录
        self.assertEqual(plan["remove_dirs"], [])

    def test_switches_off_produce_empty_plan(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        _touch(folder / "Movie (2020).srt")
        config = self.config.merged(delete_media_sidecars=False)
        plan = plan_media_cleanup(self._media(movie), config)
        self.assertEqual(plan["sidecar_files"], [])
        self.assertEqual(plan["remove_dirs"], [str(folder)])

        config = self.config.merged(delete_media_sidecars=False, cleanup_empty_dirs=False)
        plan = plan_media_cleanup(self._media(movie), config)
        self.assertEqual(plan["sidecar_files"], [])
        self.assertEqual(plan["remove_dirs"], [])

    def test_outside_delete_roots_is_skipped(self):
        outside = self.root.parent / "outside-media"
        cfg = self.config.merged(media_delete_roots=[str(self.root / "OnlyHere")])
        plan = plan_media_cleanup(
            MediaItem(server="Jellyfin", item_id="x", path=str(outside / "a.mkv"), item_type="Movie"),
            cfg,
        )
        self.assertEqual(plan["sidecar_files"], [])
        self.assertEqual(plan["remove_dirs"], [])
        self.assertTrue(plan["skipped"])

    def test_empty_path_is_skipped(self):
        plan = plan_media_cleanup(
            MediaItem(server="Jellyfin", item_id="x", path="", item_type="Movie"),
            self.config,
        )
        self.assertEqual(plan["sidecar_files"], [])
        self.assertTrue(plan["skipped"])

    def test_protected_paths_prevent_dir_removal(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        _touch(folder / "Movie (2020).srt")
        protected = {str(folder / "Another.Movie.mkv")}
        plan = plan_media_cleanup(self._media(movie), self.config, protected_video_paths=protected)
        self.assertEqual(plan["remove_dirs"], [])
        # 受保护路径不会进入删除计划
        self.assertNotIn(str(folder / "Another.Movie.mkv"), plan["sidecar_files"])

    def test_protected_media_itself_does_not_block_dir_removal(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        _touch(folder / "Movie (2020).srt")
        plan = plan_media_cleanup(
            self._media(movie),
            self.config,
            protected_video_paths={str(movie)},
        )
        self.assertEqual(plan["remove_dirs"], [str(folder)])


class ApplyMediaCleanupTest(CleanupTestBase):
    def test_apply_deletes_files_and_empty_dirs(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        sidecar = _touch(folder / "Movie (2020).srt")
        plan = plan_media_cleanup(self._media(movie), self.config)
        # 模拟 Jellyfin 删除媒体文件本身
        os.remove(movie)
        result = apply_media_cleanup(plan)
        self.assertFalse(sidecar.exists())
        self.assertFalse(folder.exists())
        self.assertEqual(result["deleted"], [str(sidecar)])
        self.assertEqual(result["removed_dirs"], [str(folder)])
        self.assertEqual(result["failed"], [])

    def test_apply_keeps_non_empty_dir(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        sidecar = _touch(folder / "Movie (2020).srt")
        leftover = _touch(folder / "leftover.bin")
        plan = plan_media_cleanup(self._media(movie), self.config)
        os.remove(movie)
        result = apply_media_cleanup(plan)
        self.assertFalse(sidecar.exists())
        self.assertTrue(folder.exists())
        self.assertTrue(leftover.exists())
        self.assertEqual(result["removed_dirs"], [])

    def test_apply_missing_sidecar_is_ignored(self):
        folder = self.root / "Movies" / "Movie (2020)"
        movie = _touch(folder / "Movie (2020).mkv")
        sidecar = _touch(folder / "Movie (2020).srt")
        plan = plan_media_cleanup(self._media(movie), self.config)
        os.remove(sidecar)  # 执行前文件已经不存在
        result = apply_media_cleanup(plan)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["failed"], [])

    def test_apply_reports_directory_target_as_failure(self):
        folder = self.root / "Movies" / "Movie (2020)"
        folder.mkdir(parents=True)
        tricky = folder / "tricky.srt"
        tricky.mkdir()  # 计划里不会出现，但执行层必须拒绝删目录
        result = apply_media_cleanup({"sidecar_files": [str(tricky)], "remove_dirs": []})
        self.assertEqual(result["deleted"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["path"], str(tricky))

    def test_apply_bottom_up_dir_order(self):
        root_dir = self.root / "Movies" / "A"
        child = root_dir / "Sub"
        _touch(child / "x.srt")
        result = apply_media_cleanup(
            {"sidecar_files": [str(child / "x.srt")], "remove_dirs": [str(root_dir), str(child)]}
        )
        self.assertEqual(result["removed_dirs"], [str(child), str(root_dir)])

    def test_apply_none_plan(self):
        result = apply_media_cleanup(None)
        self.assertEqual(result, {"deleted": [], "failed": [], "removed_dirs": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
