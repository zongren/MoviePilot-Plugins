"""下载目录临时/残留文件扫描单元测试。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(TESTS_DIR))

from config import PluginConfig  # noqa: E402
from models import TorrentItem  # noqa: E402
import residue  # noqa: E402


class TempFileScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msguard-residue-")
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self):
        return PluginConfig(download_delete_roots=[str(self.downloads)])

    def _torrent(self, hash_="h1", name="Pack", save_path=None):
        save = str(save_path or self.downloads)
        return TorrentItem(
            downloader="主QB",
            hash=hash_,
            name=name,
            save_path=save,
            content_path=save,
            progress=1.0,
        )

    def test_orphan_owned_and_check_failed(self):
        pack = self.downloads / "[Pack] Show.S01"
        pack.mkdir()
        owned = pack / "Show.S01E01.mkv.!qB"
        owned.write_bytes(b"owned")
        orphan = self.downloads / "junk.mkv.!qB"
        orphan.write_bytes(b"junk")
        other_dir = self.downloads / "other"
        other_dir.mkdir()
        failed = other_dir / "unknown.mkv.!qB"
        failed.write_bytes(b"unknown")

        def provider(downloader, torrent_hash):
            if torrent_hash == "h1":
                return ["[Pack] Show.S01/Show.S01E01.mkv"]
            return None

        result = residue.scan_temp_files(
            self._config(),
            [self._torrent(), self._torrent(hash_="h2", name="Other", save_path=other_dir)],
            provider,
        )
        rows = {row.name: row for row in result.rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["Show.S01E01.mkv.!qB"].reason, "owned_by_torrent")
        self.assertFalse(rows["Show.S01E01.mkv.!qB"].deletable)
        self.assertEqual(rows["junk.mkv.!qB"].reason, "orphan_temp_file")
        self.assertTrue(rows["junk.mkv.!qB"].deletable)
        # 第二个临时文件没有对应任务清单 → 归属校验失败，不允许删除
        self.assertEqual(rows["unknown.mkv.!qB"].reason, "owner_check_failed")
        self.assertFalse(rows["unknown.mkv.!qB"].deletable)
        self.assertEqual(result.stats["temp_files"], 3)
        self.assertEqual(result.stats["orphan_temp_files"], 1)

    def test_non_temp_files_are_ignored(self):
        (self.downloads / "movie.mkv").write_bytes(b"video")
        result = residue.scan_temp_files(self._config(), [], None)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.stats["temp_files"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
