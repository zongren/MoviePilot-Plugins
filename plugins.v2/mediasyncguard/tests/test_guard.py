"""MediaSyncGuard ``guard.py`` 单元测试（仅标准库）。

覆盖：忽略类目/标签、确定性 action_id、方向 A/B 的保守级联规则、
批量保护、dry_run 标记、already_pending 去重、cross-seed 共享 inode 保护、
safe_delete_files_map、执行前 recheck、扫描结果 JSON 结构。

运行：``python3 plugins.v2/mediasyncguard/tests/test_guard.py``
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from config import PluginConfig  # noqa: E402
from models import (  # noqa: E402
    LinkIndex,
    MediaItem,
    PendingAction,
    Snapshot,
    TorrentItem,
)
from state import SnapshotDiff  # noqa: E402
import guard  # noqa: E402
from guard import (  # noqa: E402
    ACTION_ID_PREFIX,
    BLOCKED_ALREADY_PENDING,
    BLOCKED_IGNORED_CATEGORY,
    BLOCKED_IGNORED_TAG,
    BLOCKED_MEDIA_ALREADY_GONE,
    BLOCKED_NO_INODE,
    BLOCKED_NO_TORRENT_LINK,
    BLOCKED_TORRENT_STILL_HAS_MEDIA,
    BLOCKED_TORRENT_STILL_SHARED,
    BLOCKED_UNFINISHED,
    REASON_MEDIA_DELETED,
    REASON_TORRENT_DELETED,
    action_id_for,
    build_scan,
    is_ignored,
    plan_cycle,
    recheck_media_action,
    recheck_torrent_action,
    safe_delete_files_map,
)

NOW = 1_700_000_000.0
DEV = 7


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------
def make_config(**overrides) -> PluginConfig:
    """构造测试用配置：默认 dry_run=False、自动开关关闭、阈值放大，便于精确断言。"""
    config = PluginConfig()
    config.dry_run = False
    config.auto_media_to_torrent = False
    config.auto_torrent_to_media = False
    config.auto_delete_files = True
    config.delete_torrent_files = True
    config.ignore_categories = ["KEEP"]
    config.ignore_tags = []
    config.batch_limit_media = 100
    config.batch_limit_torrents = 100
    config.grace_seconds = 600
    config.media_delete_roots = ["/medias"]
    config.scan_include_unfinished = False
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


def make_media(
    item_id: str = "m1",
    inode: int = 1001,
    dev: int = DEV,
    path: str = "",
    server: str = "Jellyfin",
    item_type: str = "Movie",
    name: str = "",
    series_name: str = "",
    season: int = 0,
    episode: int = 0,
    size: int = 123,
) -> MediaItem:
    return MediaItem(
        server=server,
        item_id=item_id,
        path=path or f"/medias/movies/{item_id}.mkv",
        inode=inode,
        dev=dev,
        size=size,
        name=name or item_id,
        item_type=item_type,
        series_name=series_name,
        season=season,
        episode=episode,
    )


def make_torrent(
    hash_: str = "h1",
    downloader: str = "主QB",
    category: str = "",
    tags=(),
    content_path: str = "",
    name: str = "",
    size: int = 100,
    progress: float = 1.0,
    state: str = "uploading",
    unsupported_path: bool = False,
    path_exists: bool = True,
) -> TorrentItem:
    return TorrentItem(
        downloader=downloader,
        hash=hash_,
        name=name or f"Torrent.{hash_}",
        category=category,
        tags=list(tags),
        state=state,
        progress=progress,
        size=size,
        save_path="/medias/Downloads",
        content_path=content_path or f"/medias/Downloads/{hash_}.mkv",
        added_on=1,
        completion_on=2,
        unsupported_path=unsupported_path,
        path_exists=path_exists,
    )


def make_snapshot(media=(), torrents=()) -> Snapshot:
    return Snapshot(
        media={item.key: item for item in media},
        torrents={item.key: item for item in torrents},
    )


def make_index(media_inodes=(), torrent_inodes=(), links=(), evidence: str = "inode") -> LinkIndex:
    """media_inodes: [(MediaItem, InodeKey)]；torrent_inodes: [(TorrentItem, InodeKey)]；
    links: [(MediaItem, TorrentItem)]。"""
    index = LinkIndex()
    for media, inode_key in media_inodes:
        index.add_media_inode(inode_key, media)
    for torrent, inode_key in torrent_inodes:
        index.add_torrent_inode(inode_key, torrent)
    for media, torrent in links:
        index.add_link(media, torrent, evidence=evidence)
    return index


def media_payload(media: MediaItem):
    return {
        "server": media.server,
        "item_id": media.item_id,
        "path": media.path,
        "name": media.name,
        "item_type": media.item_type,
        "series_name": media.series_name,
        "season": media.season,
        "episode": media.episode,
        "inode": media.inode,
        "dev": media.dev,
    }


def torrent_action(payload=None, kind: str = "delete_torrents", action_id: str = "torrents-x"):
    return PendingAction(
        action_id=action_id,
        kind=kind,
        created_at=0.0,
        due_at=0.0,
        reason="测试",
        payload={
            "torrents": [
                {"downloader": "主QB", "hash": "h1", "name": "t", "size": 1, "delete_files": True}
            ]
        }
        if payload is None
        else payload,
    )


def media_action(items=None, kind: str = "delete_media", action_id: str = "media-x"):
    return PendingAction(
        action_id=action_id,
        kind=kind,
        created_at=0.0,
        due_at=0.0,
        reason="测试",
        payload={"items": list(items or [])},
    )


# ---------------------------------------------------------------------------
# is_ignored
# ---------------------------------------------------------------------------
class IsIgnoredTest(unittest.TestCase):
    def test_default_category_keep_is_ignored(self):
        torrent = make_torrent(category="KEEP")
        self.assertEqual(is_ignored(torrent, make_config()), BLOCKED_IGNORED_CATEGORY)

    def test_category_match_is_case_insensitive_and_trimmed(self):
        torrent = make_torrent(category=" keep ")
        self.assertEqual(is_ignored(torrent, make_config()), BLOCKED_IGNORED_CATEGORY)

    def test_tag_match(self):
        torrent = make_torrent(tags=["MoviePilot自动辅种", "保种"])
        config = make_config(ignore_tags=["保种"])
        self.assertEqual(is_ignored(torrent, config), BLOCKED_IGNORED_TAG)

    def test_string_tags_are_split(self):
        torrent = TorrentItem(downloader="主QB", hash="h1", tags="MoviePilot自动辅种,保种")
        config = make_config(ignore_tags=["保种"])
        self.assertEqual(is_ignored(torrent, config), BLOCKED_IGNORED_TAG)

    def test_no_match_returns_none(self):
        torrent = make_torrent(category="TV", tags=["MoviePilot自动辅种"])
        self.assertIsNone(is_ignored(torrent, make_config()))

    def test_empty_values_are_not_ignored(self):
        torrent = make_torrent(category="", tags=[])
        self.assertIsNone(is_ignored(torrent, make_config(ignore_categories=[], ignore_tags=[])))

    def test_empty_patterns_do_not_match_everything(self):
        torrent = make_torrent(category="TV", tags=["x"])
        config = make_config(ignore_categories=[""], ignore_tags=["  "])
        self.assertIsNone(is_ignored(torrent, config))

    def test_none_torrent(self):
        self.assertIsNone(is_ignored(None, make_config()))


class ActionIdTest(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            action_id_for("torrents", ["主QB|h1"]),
            action_id_for("torrents", ["主QB|h1"]),
        )

    def test_order_independent_and_deduplicated(self):
        self.assertEqual(
            action_id_for("media", ["b", "a", "a"]),
            action_id_for("media", ["a", "b"]),
        )

    def test_kind_changes_id(self):
        self.assertNotEqual(
            action_id_for("torrents", ["a"]),
            action_id_for("media", ["a"]),
        )
        self.assertTrue(action_id_for("torrents", ["a"]).startswith(ACTION_ID_PREFIX["torrents"] + "-"))
        self.assertTrue(action_id_for("media", ["a"]).startswith(ACTION_ID_PREFIX["media"] + "-"))

    def test_unknown_kind_kept_as_prefix(self):
        self.assertTrue(action_id_for("custom_kind", ["a"]).startswith("custom_kind-"))


# ---------------------------------------------------------------------------
# 方向 A：媒体消失 -> 删种子
# ---------------------------------------------------------------------------
class DirectionATest(unittest.TestCase):
    def setUp(self):
        self.media = make_media("m1", inode=1001)
        self.torrent = make_torrent("h1", name="Movie.2020.1080p", size=999)
        self.previous = make_snapshot(media=[self.media], torrents=[self.torrent])
        self.previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(self.torrent, self.media.inode_key)],
            links=[(self.media, self.torrent)],
        )
        self.current = make_snapshot(torrents=[self.torrent])  # 媒体已消失
        self.current_links = make_index(torrent_inodes=[(self.torrent, self.media.inode_key)])
        self.diff = SnapshotDiff(lost_media=[self.media])

    def plan(
        self,
        config=None,
        *,
        current=None,
        current_links=None,
        previous=None,
        previous_links=None,
        diff=None,
        existing=None,
    ):
        return plan_cycle(
            config if config is not None else make_config(auto_media_to_torrent=True),
            diff if diff is not None else self.diff,
            previous if previous is not None else self.previous,
            current if current is not None else self.current,
            previous_links if previous_links is not None else self.previous_links,
            current_links if current_links is not None else self.current_links,
            existing_action_ids=existing,
            now=NOW,
        )

    def test_all_linked_media_gone_generates_action(self):
        plan = self.plan()
        self.assertEqual(len(plan.torrent_candidates), 1)
        candidate = plan.torrent_candidates[0]
        self.assertTrue(candidate.auto_eligible)
        self.assertEqual(candidate.blocked_reason, "")
        self.assertEqual(candidate.reason, REASON_MEDIA_DELETED)
        self.assertEqual([ref.key for ref in candidate.linked_media], [self.media.key])
        self.assertTrue(candidate.delete_files)
        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.kind, "delete_torrents")
        self.assertEqual(action.action_id, action_id_for("torrents", [self.torrent.key]))
        self.assertEqual(action.created_at, NOW)
        self.assertEqual(action.due_at, NOW + 600)
        self.assertFalse(action.dry_run)
        self.assertEqual(action.status, "pending")
        self.assertEqual(action.reason, "媒体条目已从 Jellyfin 删除")
        entry = action.payload["torrents"][0]
        self.assertEqual(entry["downloader"], "主QB")
        self.assertEqual(entry["hash"], "h1")
        self.assertEqual(entry["name"], "Movie.2020.1080p")
        self.assertEqual(entry["size"], 999)
        self.assertTrue(entry["delete_files"])
        self.assertEqual(
            plan.summary,
            {
                "lost_media": 1,
                "lost_torrents": 0,
                "torrent_candidates": 1,
                "media_candidates": 0,
                "actions": 1,
                "blocked": 0,
            },
        )

    def test_other_media_still_linked_blocks_and_no_action(self):
        other = make_media("m2", inode=1002)
        current = make_snapshot(media=[other], torrents=[self.torrent])
        current_links = make_index(
            media_inodes=[(other, other.inode_key)],
            torrent_inodes=[(self.torrent, self.media.inode_key)],
            links=[(other, self.torrent)],
        )
        plan = self.plan(current=current, current_links=current_links)
        self.assertEqual(len(plan.torrent_candidates), 1)
        candidate = plan.torrent_candidates[0]
        self.assertEqual(candidate.blocked_reason, BLOCKED_TORRENT_STILL_HAS_MEDIA)
        self.assertFalse(candidate.auto_eligible)
        self.assertEqual(plan.actions, [])
        self.assertEqual(plan.summary["blocked"], 1)

    def test_ignored_category_blocks(self):
        torrent = make_torrent("h1", category="KEEP")
        previous = make_snapshot(media=[self.media], torrents=[torrent])
        current = make_snapshot(torrents=[torrent])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(torrent, self.media.inode_key)],
            links=[(self.media, torrent)],
        )
        current_links = make_index(torrent_inodes=[(torrent, self.media.inode_key)])
        plan = self.plan(
            previous=previous, previous_links=previous_links, current=current, current_links=current_links
        )
        self.assertEqual(plan.torrent_candidates[0].blocked_reason, BLOCKED_IGNORED_CATEGORY)
        self.assertEqual(plan.actions, [])

    def test_ignored_tag_blocks(self):
        torrent = make_torrent("h1", tags=["保护"])
        config = make_config(auto_media_to_torrent=True, ignore_tags=["保护"])
        previous = make_snapshot(media=[self.media], torrents=[torrent])
        current = make_snapshot(torrents=[torrent])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(torrent, self.media.inode_key)],
            links=[(self.media, torrent)],
        )
        current_links = make_index(torrent_inodes=[(torrent, self.media.inode_key)])
        plan = self.plan(
            config,
            previous=previous,
            previous_links=previous_links,
            current=current,
            current_links=current_links,
        )
        self.assertEqual(plan.torrent_candidates[0].blocked_reason, BLOCKED_IGNORED_TAG)
        self.assertEqual(plan.actions, [])

    def test_unfinished_torrent_blocked_when_config_requires_completed(self):
        torrent = make_torrent("h1", progress=0.5)
        current = make_snapshot(torrents=[torrent])
        previous = make_snapshot(media=[self.media], torrents=[torrent])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(torrent, self.media.inode_key)],
            links=[(self.media, torrent)],
        )
        current_links = make_index(torrent_inodes=[(torrent, self.media.inode_key)])
        config = make_config(auto_media_to_torrent=True)
        config.only_completed_torrents = True  # 兼容未来配置字段
        plan = self.plan(
            config,
            previous=previous,
            previous_links=previous_links,
            current=current,
            current_links=current_links,
        )
        self.assertEqual(plan.torrent_candidates[0].blocked_reason, BLOCKED_UNFINISHED)
        self.assertEqual(plan.actions, [])

    def test_batch_limit_media_skips_entire_direction(self):
        media2 = make_media("m2", inode=1002)
        plan = self.plan(
            make_config(auto_media_to_torrent=True, batch_limit_media=2),
            diff=SnapshotDiff(lost_media=[self.media, media2]),
        )
        self.assertEqual(plan.torrent_candidates, [])
        self.assertEqual(plan.actions, [])
        self.assertEqual(len(plan.notices), 1)
        self.assertIn("批量", plan.notices[0])
        self.assertIn("2", plan.notices[0])

    def test_batch_limit_zero_disables_protection(self):
        media2 = make_media("m2", inode=2002)
        torrent2 = make_torrent("h2")
        previous = make_snapshot(media=[self.media, media2], torrents=[self.torrent, torrent2])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key), (media2, media2.inode_key)],
            torrent_inodes=[
                (self.torrent, self.media.inode_key),
                (torrent2, media2.inode_key),
            ],
            links=[(self.media, self.torrent), (media2, torrent2)],
        )
        current = make_snapshot(torrents=[self.torrent, torrent2])
        current_links = make_index(
            torrent_inodes=[
                (self.torrent, self.media.inode_key),
                (torrent2, media2.inode_key),
            ]
        )
        config = make_config(auto_media_to_torrent=True, batch_limit_media=0)
        plan = self.plan(
            config,
            previous=previous,
            previous_links=previous_links,
            current=current,
            current_links=current_links,
            diff=SnapshotDiff(lost_media=[self.media, media2]),
        )
        self.assertEqual(len(plan.torrent_candidates), 2)
        self.assertEqual(len(plan.actions), 2)

    def test_auto_switch_off_returns_candidates_but_no_actions(self):
        plan = self.plan(make_config(auto_media_to_torrent=False, dry_run=True))
        self.assertEqual(len(plan.torrent_candidates), 1)
        self.assertTrue(plan.torrent_candidates[0].auto_eligible)
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("自动删种开关未开启" in notice for notice in plan.notices))

    def test_dry_run_action_is_marked(self):
        plan = self.plan(make_config(auto_media_to_torrent=True, dry_run=True))
        self.assertEqual(len(plan.actions), 1)
        self.assertTrue(plan.actions[0].dry_run)
        self.assertTrue(any("演练模式" in notice for notice in plan.notices))

    def test_real_action_not_dry_run(self):
        plan = self.plan(make_config(auto_media_to_torrent=True, dry_run=False))
        self.assertFalse(plan.actions[0].dry_run)

    def test_already_pending_blocks_duplicate_action(self):
        existing = {action_id_for("torrents", [self.torrent.key])}
        plan = self.plan(existing=existing)
        self.assertEqual(plan.torrent_candidates[0].blocked_reason, BLOCKED_ALREADY_PENDING)
        self.assertFalse(plan.torrent_candidates[0].auto_eligible)
        self.assertEqual(plan.actions, [])

    def test_existing_action_ids_accepts_list(self):
        existing = [action_id_for("torrents", [self.torrent.key])]
        plan = self.plan(existing=existing)
        self.assertEqual(plan.torrent_candidates[0].blocked_reason, BLOCKED_ALREADY_PENDING)

    def test_torrent_also_missing_is_skipped(self):
        plan = self.plan(current=make_snapshot())
        self.assertEqual(plan.torrent_candidates, [])
        self.assertEqual(plan.actions, [])

    def test_multiple_lost_media_same_torrent_single_candidate_and_action(self):
        media2 = make_media("m2", inode=1002)
        previous = make_snapshot(media=[self.media, media2], torrents=[self.torrent])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key), (media2, media2.inode_key)],
            torrent_inodes=[(self.torrent, self.media.inode_key), (self.torrent, media2.inode_key)],
            links=[(self.media, self.torrent), (media2, self.torrent)],
        )
        current_links = make_index(
            torrent_inodes=[
                (self.torrent, self.media.inode_key),
                (self.torrent, media2.inode_key),
            ]
        )
        plan = self.plan(
            previous=previous,
            previous_links=previous_links,
            current_links=current_links,
            diff=SnapshotDiff(lost_media=[self.media, media2]),
        )
        self.assertEqual(len(plan.torrent_candidates), 1)
        self.assertEqual(len(plan.torrent_candidates[0].linked_media), 2)
        self.assertEqual(len(plan.actions), 1)

    def test_delete_files_disabled_when_shared_inode_with_current_torrent(self):
        other = make_torrent("h2")
        current = make_snapshot(torrents=[self.torrent, other])
        current_links = make_index(
            torrent_inodes=[
                (self.torrent, self.media.inode_key),
                (other, self.media.inode_key),
            ]
        )
        plan = self.plan(current=current, current_links=current_links)
        self.assertFalse(plan.torrent_candidates[0].delete_files)
        self.assertFalse(plan.actions[0].payload["torrents"][0]["delete_files"])

    def test_delete_files_false_when_current_media_still_references_inode(self):
        duplicate_media = make_media("dup", inode=self.media.inode)
        current = make_snapshot(media=[duplicate_media], torrents=[self.torrent])
        current_links = make_index(
            media_inodes=[(duplicate_media, duplicate_media.inode_key)],
            torrent_inodes=[(self.torrent, self.media.inode_key)],
        )
        plan = self.plan(current=current, current_links=current_links)
        self.assertTrue(plan.torrent_candidates[0].auto_eligible)  # 没登记 link，故仍可删种
        self.assertFalse(plan.actions[0].payload["torrents"][0]["delete_files"])

    def test_config_delete_torrent_files_off_keeps_files(self):
        plan = self.plan(make_config(auto_media_to_torrent=True, delete_torrent_files=False))
        self.assertFalse(plan.actions[0].payload["torrents"][0]["delete_files"])

    def test_config_auto_delete_files_off_keeps_files(self):
        plan = self.plan(make_config(auto_media_to_torrent=True, auto_delete_files=False))
        self.assertFalse(plan.actions[0].payload["torrents"][0]["delete_files"])

    def test_lost_media_without_torrent_link_recorded_as_media_candidate(self):
        previous_links = make_index(media_inodes=[(self.media, self.media.inode_key)])
        plan = self.plan(previous_links=previous_links)
        self.assertEqual(plan.torrent_candidates, [])
        self.assertEqual(len(plan.media_candidates), 1)
        candidate = plan.media_candidates[0]
        self.assertEqual(candidate.media.key, self.media.key)
        self.assertEqual(candidate.reason, REASON_MEDIA_DELETED)
        self.assertEqual(candidate.blocked_reason, BLOCKED_NO_TORRENT_LINK)
        self.assertFalse(candidate.auto_eligible)
        self.assertEqual(plan.actions, [])

    def test_empty_diff_produces_empty_plan(self):
        plan = self.plan(diff=SnapshotDiff())
        self.assertEqual(plan.torrent_candidates, [])
        self.assertEqual(plan.media_candidates, [])
        self.assertEqual(plan.actions, [])
        self.assertEqual(plan.notices, [])
        self.assertEqual(plan.summary["lost_media"], 0)

    def test_plan_does_not_mutate_inputs(self):
        before_current = json.dumps(self.current.to_dict(), sort_keys=True)
        before_previous = json.dumps(self.previous.to_dict(), sort_keys=True)
        before_links = json.dumps(self.previous_links.to_dict(), sort_keys=True)
        self.plan()
        self.assertEqual(json.dumps(self.current.to_dict(), sort_keys=True), before_current)
        self.assertEqual(json.dumps(self.previous.to_dict(), sort_keys=True), before_previous)
        self.assertEqual(json.dumps(self.previous_links.to_dict(), sort_keys=True), before_links)


# ---------------------------------------------------------------------------
# 方向 B：种子消失 -> 删媒体
# ---------------------------------------------------------------------------
class DirectionBTest(unittest.TestCase):
    def setUp(self):
        self.media = make_media("m1", inode=1001, path="/medias/movies/m1.mkv")
        self.torrent = make_torrent("h1")
        self.previous = make_snapshot(media=[self.media], torrents=[self.torrent])
        self.previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(self.torrent, self.media.inode_key)],
            links=[(self.media, self.torrent)],
        )
        self.current = make_snapshot(media=[self.media])  # 种子已消失
        self.current_links = make_index(media_inodes=[(self.media, self.media.inode_key)])
        self.diff = SnapshotDiff(lost_torrents=[self.torrent])

    def plan(
        self,
        config=None,
        *,
        current=None,
        current_links=None,
        previous=None,
        previous_links=None,
        diff=None,
        existing=None,
    ):
        return plan_cycle(
            config if config is not None else make_config(auto_torrent_to_media=True),
            diff if diff is not None else self.diff,
            previous if previous is not None else self.previous,
            current if current is not None else self.current,
            previous_links if previous_links is not None else self.previous_links,
            current_links if current_links is not None else self.current_links,
            existing_action_ids=existing,
            now=NOW,
        )

    def test_torrent_deleted_media_eligible_and_action_generated(self):
        plan = self.plan()
        self.assertEqual(len(plan.media_candidates), 1)
        candidate = plan.media_candidates[0]
        self.assertEqual(candidate.reason, REASON_TORRENT_DELETED)
        self.assertTrue(candidate.auto_eligible)
        self.assertEqual(candidate.blocked_reason, "")
        self.assertEqual(candidate.evidence, "inode")
        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.kind, "delete_media")
        self.assertEqual(action.action_id, action_id_for("media", [self.media.key]))
        self.assertEqual(action.created_at, NOW)
        self.assertEqual(action.due_at, NOW + 600)
        self.assertEqual(action.reason, "下载任务已从下载器删除")
        self.assertFalse(action.dry_run)
        self.assertEqual(action.payload["items"], [media_payload(self.media)])

    def test_shared_inode_blocks_media_deletion(self):
        other = make_torrent("h2")
        current = make_snapshot(media=[self.media], torrents=[other])
        current_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(other, self.media.inode_key)],
        )
        plan = self.plan(current=current, current_links=current_links)
        self.assertEqual(plan.media_candidates[0].blocked_reason, BLOCKED_TORRENT_STILL_SHARED)
        self.assertFalse(plan.media_candidates[0].auto_eligible)
        self.assertEqual(plan.actions, [])

    def test_media_without_inode_blocked(self):
        media = make_media("m1", inode=0, dev=0)
        previous = make_snapshot(media=[media], torrents=[self.torrent])
        previous_links = make_index(
            torrent_inodes=[(self.torrent, (DEV, 1001))],
            links=[(media, self.torrent)],
        )
        current = make_snapshot(media=[media])
        current_links = make_index()
        plan = self.plan(
            previous=previous,
            previous_links=previous_links,
            current=current,
            current_links=current_links,
        )
        self.assertEqual(plan.media_candidates[0].blocked_reason, BLOCKED_NO_INODE)
        self.assertEqual(plan.actions, [])

    def test_ignored_lost_torrent_blocks(self):
        torrent = make_torrent("h1", category="KEEP")
        previous = make_snapshot(media=[self.media], torrents=[torrent])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(torrent, self.media.inode_key)],
            links=[(self.media, torrent)],
        )
        plan = self.plan(
            previous=previous,
            previous_links=previous_links,
            diff=SnapshotDiff(lost_torrents=[torrent]),
        )
        self.assertEqual(plan.media_candidates[0].blocked_reason, BLOCKED_IGNORED_CATEGORY)
        self.assertEqual(plan.actions, [])

    def test_batch_limit_torrents_skips_entire_direction(self):
        torrent2 = make_torrent("h2")
        plan = self.plan(
            make_config(auto_torrent_to_media=True, batch_limit_torrents=1),
            diff=SnapshotDiff(lost_torrents=[self.torrent, torrent2]),
        )
        self.assertEqual(plan.media_candidates, [])
        self.assertEqual(plan.actions, [])
        self.assertEqual(len(plan.notices), 1)
        self.assertIn("批量", plan.notices[0])

    def test_auto_switch_off_returns_candidates_but_no_actions(self):
        plan = self.plan(make_config(auto_torrent_to_media=False, dry_run=True))
        self.assertEqual(len(plan.media_candidates), 1)
        self.assertTrue(plan.media_candidates[0].auto_eligible)
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("自动删媒体开关未开启" in notice for notice in plan.notices))

    def test_dry_run_action_is_marked(self):
        plan = self.plan(make_config(auto_torrent_to_media=True, dry_run=True))
        self.assertTrue(plan.actions[0].dry_run)
        self.assertTrue(any("演练模式" in notice for notice in plan.notices))

    def test_media_already_gone_recorded_as_candidate_only(self):
        current = make_snapshot()
        current_links = make_index()
        plan = self.plan(current=current, current_links=current_links)
        self.assertEqual(plan.actions, [])
        self.assertEqual(len(plan.media_candidates), 1)
        self.assertEqual(plan.media_candidates[0].blocked_reason, BLOCKED_MEDIA_ALREADY_GONE)
        self.assertFalse(plan.media_candidates[0].auto_eligible)

    def test_already_pending_blocks_duplicate_action(self):
        existing = {action_id_for("media", [self.media.key])}
        plan = self.plan(existing=existing)
        self.assertEqual(plan.media_candidates[0].blocked_reason, BLOCKED_ALREADY_PENDING)
        self.assertEqual(plan.actions, [])

    def test_history_evidence_recorded(self):
        links = LinkIndex()
        links.add_link(self.media, self.torrent, evidence="history")
        plan = self.plan(previous_links=links)
        self.assertEqual(plan.media_candidates[0].evidence, "history")

    def test_cross_seed_duplicate_media_only_one_candidate_and_action(self):
        torrent2 = make_torrent("h2")
        previous = make_snapshot(media=[self.media], torrents=[self.torrent, torrent2])
        previous_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[
                (self.torrent, self.media.inode_key),
                (torrent2, self.media.inode_key),
            ],
            links=[(self.media, self.torrent), (self.media, torrent2)],
        )
        plan = self.plan(
            previous=previous,
            previous_links=previous_links,
            diff=SnapshotDiff(lost_torrents=[self.torrent, torrent2]),
        )
        self.assertEqual(len(plan.media_candidates), 1)
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(len(plan.actions[0].payload["items"]), 1)

    def test_no_previous_media_link_means_no_candidate(self):
        plan = self.plan(previous_links=make_index())
        self.assertEqual(plan.media_candidates, [])
        self.assertEqual(plan.actions, [])

    def test_payload_contains_episode_fields(self):
        episode = make_media(
            "ep1",
            inode=1001,
            path="/medias/TV/Show/Season 1/e01.mkv",
            item_type="Episode",
            name="e01",
            series_name="Show",
            season=1,
            episode=1,
        )
        previous = make_snapshot(media=[episode], torrents=[self.torrent])
        previous_links = make_index(
            media_inodes=[(episode, episode.inode_key)],
            torrent_inodes=[(self.torrent, episode.inode_key)],
            links=[(episode, self.torrent)],
        )
        current = make_snapshot(media=[episode])
        current_links = make_index(media_inodes=[(episode, episode.inode_key)])
        plan = self.plan(
            previous=previous,
            previous_links=previous_links,
            current=current,
            current_links=current_links,
        )
        item = plan.actions[0].payload["items"][0]
        self.assertEqual(item["server"], "Jellyfin")
        self.assertEqual(item["item_id"], "ep1")
        self.assertEqual(item["series_name"], "Show")
        self.assertEqual(item["season"], 1)
        self.assertEqual(item["episode"], 1)
        self.assertEqual(item["inode"], 1001)
        self.assertEqual(item["dev"], DEV)


class CombinedDirectionTest(unittest.TestCase):
    """同一轮里媒体删除与种子删除同时发生：两个方向互不干扰。"""

    def test_both_directions_in_same_cycle(self):
        media_lost = make_media("lost", inode=7001, path="/medias/movies/lost.mkv")
        media_alive = make_media("alive", inode=7002, path="/medias/movies/alive.mkv")
        torrent_still = make_torrent("still")
        torrent_lost = make_torrent("lost")
        previous = make_snapshot(media=[media_lost, media_alive], torrents=[torrent_still, torrent_lost])
        previous_links = make_index(
            media_inodes=[
                (media_lost, media_lost.inode_key),
                (media_alive, media_alive.inode_key),
            ],
            torrent_inodes=[
                (torrent_still, media_lost.inode_key),
                (torrent_lost, media_alive.inode_key),
            ],
            links=[(media_lost, torrent_still), (media_alive, torrent_lost)],
        )
        current = make_snapshot(media=[media_alive], torrents=[torrent_still])
        current_links = make_index(
            media_inodes=[(media_alive, media_alive.inode_key)],
            torrent_inodes=[(torrent_still, media_lost.inode_key)],
        )
        diff = SnapshotDiff(lost_media=[media_lost], lost_torrents=[torrent_lost])
        config = make_config(auto_media_to_torrent=True, auto_torrent_to_media=True)
        plan = plan_cycle(
            config, diff, previous, current, previous_links, current_links, now=NOW
        )
        self.assertEqual([item.torrent.key for item in plan.torrent_candidates], [torrent_still.key])
        self.assertEqual([item.media.key for item in plan.media_candidates], [media_alive.key])
        kinds = sorted(action.kind for action in plan.actions)
        self.assertEqual(kinds, ["delete_media", "delete_torrents"])
        self.assertEqual(plan.summary["actions"], 2)
        self.assertEqual(plan.summary["blocked"], 0)


# ---------------------------------------------------------------------------
# safe_delete_files_map
# ---------------------------------------------------------------------------
class SafeDeleteFilesMapTest(unittest.TestCase):
    def test_unknown_torrent_returns_false(self):
        self.assertEqual(safe_delete_files_map(["q|h"], LinkIndex()), {"q|h": False})

    def test_safe_when_only_self_and_no_media(self):
        torrent = make_torrent("h1")
        links = make_index(torrent_inodes=[(torrent, (DEV, 100))])
        self.assertTrue(safe_delete_files_map([torrent.key], links)[torrent.key])

    def test_unsafe_when_shared_with_other_torrent_not_in_set(self):
        torrent = make_torrent("h1")
        other = make_torrent("h2")
        links = make_index(
            torrent_inodes=[(torrent, (DEV, 100)), (other, (DEV, 100))]
        )
        result = safe_delete_files_map([torrent.key], links)
        self.assertFalse(result[torrent.key])

    def test_safe_when_sharing_torrent_also_in_delete_set(self):
        torrent = make_torrent("h1")
        other = make_torrent("h2")
        links = make_index(
            torrent_inodes=[(torrent, (DEV, 100)), (other, (DEV, 100))]
        )
        result = safe_delete_files_map([torrent.key, other.key], links)
        self.assertTrue(result[torrent.key])
        self.assertTrue(result[other.key])

    def test_unsafe_when_media_still_references_inode(self):
        media = make_media("m1", inode=100)
        torrent = make_torrent("h1")
        links = make_index(
            media_inodes=[(media, media.inode_key)],
            torrent_inodes=[(torrent, media.inode_key)],
        )
        self.assertFalse(safe_delete_files_map([torrent.key], links)[torrent.key])

    def test_unsafe_when_index_inconsistent(self):
        torrent = make_torrent("h1")
        links = LinkIndex()
        links.torrent_inodes[torrent.key] = ["7:100"]  # 只有一半索引
        self.assertFalse(safe_delete_files_map([torrent.key], links)[torrent.key])

    def test_invalid_inode_key_is_unsafe(self):
        torrent = make_torrent("h1")
        links = LinkIndex()
        links.torrent_inodes[torrent.key] = ["0:0"]
        self.assertFalse(safe_delete_files_map([torrent.key], links)[torrent.key])

    def test_multiple_inodes_all_must_be_safe(self):
        torrent = make_torrent("h1")
        other = make_torrent("h2")
        links = make_index(
            torrent_inodes=[
                (torrent, (DEV, 100)),
                (torrent, (DEV, 101)),
                (other, (DEV, 101)),
            ]
        )
        self.assertFalse(safe_delete_files_map([torrent.key], links)[torrent.key])
        # 共享种子一起删除时两个都安全
        both = safe_delete_files_map([torrent.key, other.key], links)
        self.assertTrue(both[torrent.key])
        self.assertTrue(both[other.key])

    def test_empty_input(self):
        self.assertEqual(safe_delete_files_map([], LinkIndex()), {})


# ---------------------------------------------------------------------------
# recheck_torrent_action
# ---------------------------------------------------------------------------
class RecheckTorrentActionTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.torrent = make_torrent("h1")
        self.current = make_snapshot(torrents=[self.torrent])
        self.current_links = LinkIndex()
        self.action = torrent_action()

    def test_pass(self):
        ok, reason = recheck_torrent_action(
            self.action, self.current, self.current_links, self.config
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_fail_when_torrent_missing(self):
        ok, reason = recheck_torrent_action(
            self.action, make_snapshot(), self.current_links, self.config
        )
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_fail_when_media_still_linked(self):
        media = make_media("m1")
        current = make_snapshot(media=[media], torrents=[self.torrent])
        current_links = make_index(links=[(media, self.torrent)])
        ok, reason = recheck_torrent_action(self.action, current, current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("媒体", reason)

    def test_fail_when_torrent_now_ignored(self):
        torrent = make_torrent("h1", category="KEEP")
        current = make_snapshot(torrents=[torrent])
        ok, reason = recheck_torrent_action(self.action, current, LinkIndex(), self.config)
        self.assertFalse(ok)
        self.assertIn("忽略", reason)

    def test_fail_on_kind_mismatch(self):
        ok, reason = recheck_torrent_action(
            media_action(), self.current, self.current_links, self.config
        )
        self.assertFalse(ok)
        self.assertIn("类型", reason)

    def test_fail_on_empty_targets(self):
        ok, reason = recheck_torrent_action(
            torrent_action(payload={"torrents": []}), self.current, LinkIndex(), self.config
        )
        self.assertFalse(ok)
        self.assertIn("缺少", reason)

    def test_fail_on_missing_hash(self):
        action = torrent_action(payload={"torrents": [{"downloader": "主QB"}]})
        ok, reason = recheck_torrent_action(action, self.current, LinkIndex(), self.config)
        self.assertFalse(ok)
        self.assertIn("hash", reason)

    def test_none_action(self):
        ok, _ = recheck_torrent_action(None, self.current, LinkIndex(), self.config)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# recheck_media_action
# ---------------------------------------------------------------------------
class RecheckMediaActionTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.media = make_media("m1", inode=1001, path="/medias/movies/m1.mkv")
        self.current = make_snapshot(media=[self.media])
        self.current_links = make_index(media_inodes=[(self.media, self.media.inode_key)])
        self.action = media_action([media_payload(self.media)])

    def test_pass(self):
        ok, reason = recheck_media_action(
            self.action, self.current, self.current_links, self.config
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_fail_when_media_missing(self):
        ok, reason = recheck_media_action(self.action, make_snapshot(), self.current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_fail_when_inode_shared_with_current_torrent(self):
        torrent = make_torrent("h1")
        current = make_snapshot(media=[self.media], torrents=[torrent])
        current_links = make_index(
            media_inodes=[(self.media, self.media.inode_key)],
            torrent_inodes=[(torrent, self.media.inode_key)],
        )
        ok, reason = recheck_media_action(self.action, current, current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("种子", reason)

    def test_fail_when_inode_missing(self):
        media = make_media("m1", inode=0, dev=0)
        current = make_snapshot(media=[media])
        current_links = make_index()
        action = media_action([media_payload(media)])
        ok, reason = recheck_media_action(action, current, current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("inode", reason)

    def test_fail_when_path_outside_delete_roots(self):
        media = make_media("m1", inode=1001, path="/other/movies/m1.mkv")
        current = make_snapshot(media=[media])
        current_links = make_index(media_inodes=[(media, media.inode_key)])
        action = media_action([media_payload(media)])
        ok, reason = recheck_media_action(action, current, current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("目录", reason)

    def test_prefix_collision_path_not_allowed(self):
        media = make_media("m1", inode=1001, path="/medias2/movies/m1.mkv")
        current = make_snapshot(media=[media])
        current_links = make_index(media_inodes=[(media, media.inode_key)])
        action = media_action([media_payload(media)])
        ok, _ = recheck_media_action(action, current, current_links, self.config)
        self.assertFalse(ok)

    def test_fail_on_kind_mismatch(self):
        ok, _ = recheck_media_action(torrent_action(), self.current, self.current_links, self.config)
        self.assertFalse(ok)

    def test_multiple_items_all_checked(self):
        bad = make_media("bad", inode=2002, path="/other/movies/bad.mkv")
        current = make_snapshot(media=[self.media, bad])
        current_links = make_index(
            media_inodes=[
                (self.media, self.media.inode_key),
                (bad, bad.inode_key),
            ]
        )
        action = media_action([media_payload(self.media), media_payload(bad)])
        ok, reason = recheck_media_action(action, current, current_links, self.config)
        self.assertFalse(ok)
        self.assertIn("目录", reason)

    def test_fail_on_empty_payload(self):
        ok, _ = recheck_media_action(media_action(), self.current, self.current_links, self.config)
        self.assertFalse(ok)

    def test_none_action(self):
        ok, _ = recheck_media_action(None, self.current, self.current_links, self.config)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# build_scan
# ---------------------------------------------------------------------------
class BuildScanTest(unittest.TestCase):
    def setUp(self):
        self.now = 1234.5
        self.media_linked = make_media(
            "linked", inode=5001, path="/medias/Movies/Linked/linked.mkv", name="Linked", size=111
        )
        self.media_ep1 = make_media(
            "e1",
            inode=5002,
            path="/medias/TV/Show/Season 1/e01.mkv",
            item_type="Episode",
            name="e01",
            series_name="Show",
            season=1,
            episode=1,
            size=100,
        )
        self.media_ep2 = make_media(
            "e2",
            inode=5003,
            path="/medias/TV/Show/Season 1/e02.mkv",
            item_type="Episode",
            name="e02",
            series_name="Show",
            season=1,
            episode=2,
            size=50,
        )
        self.media_movie = make_media(
            "mv",
            inode=5004,
            path="/medias/Movies/Movie (2020)/movie.mkv",
            name="movie",
            size=10,
        )
        self.torrents = {
            "linked": make_torrent("linkedhash", category="TV", name="Linked"),
            "unlinked": make_torrent("unlinked", category="TV", name="Unlinked", size=1000),
            "shared1": make_torrent("shared1", category="Movie", name="Shared1", size=300),
            "shared2": make_torrent("shared2", category="Movie", name="Shared2", size=200),
            "downloading": make_torrent("downloading", progress=0.5, category="TV", name="Down"),
            "ignored": make_torrent("ignored", category="KEEP", name="Ignored", size=400),
            "novideo": make_torrent("novideo", category="TV", name="NoVideo"),
            "missing": make_torrent(
                "missing", category="TV", name="Missing", size=77, path_exists=False
            ),
            "empty": make_torrent("empty", category="TV", name="Empty", content_path=" "),
            "unsupported": make_torrent(
                "unsupported",
                downloader="副QB",
                category="TV",
                name="Unsupported",
                unsupported_path=True,
                content_path="/invisible/x",
            ),
        }
        self.snapshot = Snapshot(
            media={
                item.key: item
                for item in (
                    self.media_linked,
                    self.media_ep1,
                    self.media_ep2,
                    self.media_movie,
                )
            },
            torrents={torrent.key: torrent for torrent in self.torrents.values()},
        )
        linked = self.torrents["linked"]
        self.links = make_index(
            media_inodes=[
                (self.media_linked, self.media_linked.inode_key),
                (self.media_ep1, self.media_ep1.inode_key),
                (self.media_ep2, self.media_ep2.inode_key),
                (self.media_movie, self.media_movie.inode_key),
            ],
            torrent_inodes=[
                (linked, self.media_linked.inode_key),
                (self.torrents["unlinked"], (DEV, 6001)),
                (self.torrents["unlinked"], (DEV, 6002)),
                (self.torrents["shared1"], (DEV, 6003)),
                (self.torrents["shared2"], (DEV, 6003)),
                (self.torrents["downloading"], (DEV, 6004)),
                (self.torrents["ignored"], (DEV, 6005)),
            ],
            links=[(self.media_linked, linked)],
        )
        self.history_rows = [
            ("主QB", "hist1", "/medias/TV/Show/Season 1/./e01.mkv"),
            ("主QB", "hist2", "/medias/not-in-library.mkv"),
        ]

    def scan(self, config=None, **kwargs):
        return build_scan(
            config if config is not None else make_config(),
            self.snapshot,
            self.links,
            history_rows=self.history_rows,
            now=self.now,
            **kwargs,
        )

    def test_top_level_shape_and_json_serializable(self):
        result = self.scan()
        self.assertEqual(
            set(result.keys()),
            {
                "generated_at",
                "duration",
                "stats",
                "media_without_torrent",
                "media_groups",
                "torrents_without_media",
                "torrent_groups",
                "skipped",
            },
        )
        self.assertEqual(result["generated_at"], self.now)
        json.dumps(result)  # 必须可 JSON 序列化

    def test_media_without_torrent(self):
        result = self.scan()
        rows = {row["id"]: row for row in result["media_without_torrent"]}
        self.assertEqual(set(rows.keys()), {self.media_ep1.key, self.media_ep2.key, self.media_movie.key})
        ep1 = rows[self.media_ep1.key]
        self.assertEqual(ep1["reason"], "no_torrent_link")
        self.assertEqual(ep1["display_title"], "Show S01 E01")
        self.assertEqual(ep1["folder"], "/medias/TV/Show/Season 1")
        self.assertEqual(ep1["history_hashes"], ["hist1"])
        self.assertEqual(ep1["inode"], 5002)
        self.assertEqual(ep1["dev"], DEV)
        self.assertEqual(ep1["size"], 100)
        self.assertEqual(rows[self.media_ep2.key]["history_hashes"], [])
        self.assertEqual(rows[self.media_movie.key]["folder"], "/medias/Movies/Movie (2020)")

    def test_media_groups(self):
        result = self.scan()
        groups = {group["group_id"]: group for group in result["media_groups"]}
        tv_group = groups["folder:/medias/TV/Show/Season 1"]
        self.assertEqual(tv_group["title"], "Show S01")
        self.assertEqual(tv_group["item_type"], "Episode")
        self.assertEqual(tv_group["count"], 2)
        self.assertEqual(tv_group["size"], 150)
        self.assertEqual(len(tv_group["item_ids"]), 2)
        movie_group = groups["folder:/medias/Movies/Movie (2020)"]
        self.assertEqual(movie_group["count"], 1)
        self.assertEqual(movie_group["size"], 10)

    def test_torrents_without_media(self):
        result = self.scan()
        rows = {row["id"]: row for row in result["torrents_without_media"]}
        # 已关联媒体的种子不出现
        self.assertNotIn(self.torrents["linked"].key, rows)
        # 无媒体关联的种子出现
        self.assertIn(self.torrents["unlinked"].key, rows)
        unlinked = rows[self.torrents["unlinked"].key]
        self.assertEqual(unlinked["reason"], "no_media_link")
        self.assertEqual(unlinked["video_files"], 2)
        self.assertEqual(unlinked["shared_torrents"], 0)
        self.assertTrue(unlinked["safe_to_delete_files"])
        self.assertTrue(unlinked["completed"])
        self.assertEqual(unlinked["downloader"], "主QB")
        self.assertEqual(unlinked["category"], "TV")
        # 共享 inode：unsafe + shared_torrents=1
        shared1 = rows[self.torrents["shared1"].key]
        shared2 = rows[self.torrents["shared2"].key]
        self.assertEqual(shared1["shared_torrents"], 1)
        self.assertFalse(shared1["safe_to_delete_files"])
        self.assertEqual(shared2["shared_torrents"], 1)
        self.assertFalse(shared2["safe_to_delete_files"])
        # 忽略类目：仍列出但带 ignored_reason
        ignored = rows[self.torrents["ignored"].key]
        self.assertEqual(ignored["ignored_reason"], BLOCKED_IGNORED_CATEGORY)
        # 内容缺失
        missing = rows[self.torrents["missing"].key]
        self.assertEqual(missing["reason"], "missing_content")
        self.assertFalse(missing["path_exists"])
        self.assertFalse(missing["safe_to_delete_files"])
        # 未完成/无视频/空路径/不支持 不进列表
        for key in ("downloading", "novideo", "empty", "unsupported"):
            self.assertNotIn(self.torrents[key].key, rows)

    def test_skipped_counts(self):
        skipped = self.scan()["skipped"]
        self.assertEqual(
            skipped,
            {"downloading": 1, "no_video": 1, "ignored": 1, "missing_path": 1, "unsupported": 1},
        )

    def test_torrent_groups(self):
        result = self.scan()
        groups = {group["group_id"]: group for group in result["torrent_groups"]}
        # TV: unlinked(1000) + missing(77)
        self.assertEqual(groups["category:TV"]["count"], 2)
        self.assertEqual(groups["category:TV"]["size"], 1077)
        # Movie: shared1 + shared2
        self.assertEqual(groups["category:Movie"]["count"], 2)
        self.assertEqual(groups["category:Movie"]["size"], 500)
        self.assertEqual(groups["category:KEEP"]["count"], 1)
        self.assertEqual(groups["category:KEEP"]["size"], 400)

    def test_stats_estimated_from_snapshot_and_links(self):
        stats = self.scan()["stats"]
        self.assertEqual(stats["media_total"], 4)
        self.assertEqual(stats["torrent_total"], 10)
        self.assertEqual(stats["link_pairs"], 1)
        self.assertEqual(stats["video_files"], 7)
        self.assertEqual(stats["torrents_unsupported"], 1)
        self.assertEqual(stats["torrents_missing_path"], 1)
        self.assertEqual(stats["torrents_walked"], 8)
        self.assertEqual(stats["walk_errors"], 0)

    def test_stats_override_and_duration(self):
        stats = {
            "torrents_walked": 7,
            "walk_errors": 3,
            "linked_pairs": 42,
            "torrents_total": 10,
        }
        result = self.scan(stats=stats, duration=8.5)
        self.assertEqual(result["duration"], 8.5)
        self.assertEqual(result["stats"]["torrents_walked"], 7)
        self.assertEqual(result["stats"]["walk_errors"], 3)
        self.assertEqual(result["stats"]["link_pairs"], 42)
        self.assertEqual(result["stats"]["torrent_total"], 10)

    def test_stats_accepts_object_with_to_dict(self):
        class FakeStats:
            def to_dict(self):
                return {"media_total": 99, "linked_pairs": 98, "torrents_total": 97}

        result = self.scan(stats=FakeStats())
        self.assertEqual(result["stats"]["media_total"], 99)
        self.assertEqual(result["stats"]["link_pairs"], 98)
        self.assertEqual(result["stats"]["torrent_total"], 97)

    def test_scan_include_unfinished_lists_downloading(self):
        result = self.scan(make_config(scan_include_unfinished=True))
        rows = {row["id"]: row for row in result["torrents_without_media"]}
        self.assertIn(self.torrents["downloading"].key, rows)
        self.assertEqual(rows[self.torrents["downloading"].key]["reason"], "downloading")
        self.assertEqual(result["skipped"]["downloading"], 0)

    def test_empty_snapshot(self):
        result = build_scan(make_config(), Snapshot(), LinkIndex(), now=self.now)
        self.assertEqual(result["media_without_torrent"], [])
        self.assertEqual(result["media_groups"], [])
        self.assertEqual(result["torrents_without_media"], [])
        self.assertEqual(result["torrent_groups"], [])
        self.assertEqual(result["stats"]["media_total"], 0)
        self.assertEqual(result["stats"]["torrent_total"], 0)
        json.dumps(result)

    def test_scan_does_not_mutate_inputs(self):
        before_snapshot = json.dumps(self.snapshot.to_dict(), sort_keys=True)
        before_links = json.dumps(self.links.to_dict(), sort_keys=True)
        self.scan()
        self.assertEqual(json.dumps(self.snapshot.to_dict(), sort_keys=True), before_snapshot)
        self.assertEqual(json.dumps(self.links.to_dict(), sort_keys=True), before_links)

    def test_media_without_path_is_listed(self):
        orphan = MediaItem(server="Jellyfin", item_id="nopath", path="", inode=0, dev=0, name="空路径")
        snapshot = Snapshot(media={orphan.key: orphan})
        result = build_scan(make_config(), snapshot, LinkIndex(), now=self.now)
        rows = {row["id"]: row for row in result["media_without_torrent"]}
        self.assertIn(orphan.key, rows)
        self.assertEqual(rows[orphan.key]["folder"], "")
        self.assertEqual(rows[orphan.key]["history_hashes"], [])

    def test_scan_result_to_dict_keys(self):
        result = guard.ScanResult(generated_at=1.0, duration=2.0)
        data = result.to_dict()
        self.assertEqual(data["generated_at"], 1.0)
        self.assertEqual(data["duration"], 2.0)
        self.assertIn("skipped", data)
        json.dumps(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
