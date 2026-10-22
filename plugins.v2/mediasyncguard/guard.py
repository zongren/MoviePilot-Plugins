"""MediaSyncGuard 安全决策核心模块。

本模块只依赖 Python 标准库与本插件的 ``models.py`` / ``config.py`` / ``state.py``，
**禁止**导入任何 MoviePilot（``app.*``）模块；也不做任何网络/真实文件操作，
只根据快照与硬链接索引计算“保守级联删除”的候选项、待执行动作与扫描结果。

用户已确认的最保守级联规则：

- 媒体删除 -> 仅当该种子**所有**关联媒体都消失时才删除种子；
- 种子删除 -> 仅当**没有其它现存种子**引用同一文件（inode）时才删除媒体；
- 批量变化（疑似媒体库刷新/批量删种）直接跳过自动处理，只提示人工确认；
- 命中忽略类目/标签、无法校验 inode、共享 inode 等情况一律不自动处理；
- 所有动作在执行前必须再经过 :func:`recheck_torrent_action` /
  :func:`recheck_media_action` 复核。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # MoviePilot 中作为包模块导入：mediasyncguard.guard
    from .config import PluginConfig
    from .models import (
        LinkIndex,
        MediaItem,
        MediaRef,
        PendingAction,
        Snapshot,
        TorrentItem,
        TorrentRef,
        media_key,
        parse_inode_key,
        torrent_key,
    )
    from .state import SnapshotDiff
except ImportError:  # 单元测试/独立脚本：插件目录已加入 sys.path，按顶层模块导入
    from config import PluginConfig
    from models import (
        LinkIndex,
        MediaItem,
        MediaRef,
        PendingAction,
        Snapshot,
        TorrentItem,
        TorrentRef,
        media_key,
        parse_inode_key,
        torrent_key,
    )
    from state import SnapshotDiff

logger = logging.getLogger(__name__)

# 动作 ID 前缀（与 action kind 区分：delete_torrents / delete_media）
ACTION_ID_PREFIX = {"torrents": "torrents", "media": "media"}

# 候选理由（供 runtime/前端展示，注意与契约一致）
REASON_MEDIA_DELETED = "media_deleted"
REASON_TORRENT_DELETED = "torrent_deleted"

# 候选被阻止原因
BLOCKED_IGNORED_CATEGORY = "ignored_category"
BLOCKED_IGNORED_TAG = "ignored_tag"
BLOCKED_TORRENT_STILL_HAS_MEDIA = "torrent_still_has_media"
BLOCKED_TORRENT_STILL_SHARED = "torrent_still_shared"
BLOCKED_UNFINISHED = "unfinished"
BLOCKED_ALREADY_PENDING = "already_pending"
BLOCKED_NO_TORRENT_LINK = "no_torrent_link"
BLOCKED_NO_MEDIA_LINK = "no_media_link"
BLOCKED_NO_INODE = "no_inode"
BLOCKED_MEDIA_ALREADY_GONE = "media_already_gone"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def action_id_for(kind: str, keys: Iterable[str]) -> str:
    """根据动作类型与目标键集合生成确定性动作 ID。

    同一个待删目标集合在任意时刻生成的 ID 都相同，便于跨轮询去重
    （``already_pending`` 判定）以及 runtime 恢复状态后继续跟踪。
    """
    normalized = sorted({str(key) for key in (keys or []) if str(key)})
    raw = f"{str(kind or '')}:" + "|".join(normalized)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    prefix = ACTION_ID_PREFIX.get(str(kind or ""), str(kind or "action"))
    return f"{prefix}-{digest}"


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _match_any(value: Any, patterns: Iterable[Any]) -> bool:
    """忽略大小写/首尾空白的精确匹配。"""
    target = _normalize_text(value)
    if not target:
        return False
    for pattern in patterns or []:
        if _normalize_text(pattern) == target:
            return True
    return False


def _torrent_tags(torrent: TorrentItem) -> List[str]:
    """稳健地取种子标签列表（兼容 qB 原始逗号分隔字符串）。"""
    raw = getattr(torrent, "tags", None)
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
        return [part.strip() for part in parts if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(tag).strip() for tag in raw if str(tag).strip()]
    return []


def is_ignored(torrent: TorrentItem, config: PluginConfig) -> Optional[str]:
    """判断种子是否命中忽略规则。

    返回 ``"ignored_category"``（类目命中）、``"ignored_tag"``（标签命中）或 ``None``。
    命中任一项的种子永远不会被自动删除。
    """
    if torrent is None:
        return None
    config = config or PluginConfig()
    if _match_any(torrent.category, config.ignore_categories):
        return BLOCKED_IGNORED_CATEGORY
    for tag in _torrent_tags(torrent):
        if _match_any(tag, config.ignore_tags):
            return BLOCKED_IGNORED_TAG
    return None


def _path_under_roots(path: str, roots: Optional[Iterable[str]]) -> bool:
    """判断 ``path`` 是否位于任一允许根目录内（按路径段匹配，防 ``/medias2`` 误伤）。"""
    if not path:
        return False
    try:
        target = os.path.normpath(str(path))
    except (TypeError, ValueError):
        return False
    for root in roots or []:
        if not root:
            continue
        try:
            normalized_root = os.path.normpath(str(root))
        except (TypeError, ValueError):
            continue
        if normalized_root in ("", os.sep):
            return True
        if target == normalized_root:
            return True
        if target.startswith(normalized_root.rstrip(os.sep) + os.sep):
            return True
    return False


def _evidence_of(links: LinkIndex, media: MediaItem, torrent: TorrentItem) -> str:
    """取媒体与种子关联的证据类型（inode 优先于 history）。"""
    pair_key = f"{media.key}->{torrent.key}"
    return (links.evidence.get(pair_key) or "inode") if links is not None else "inode"


def _grace_seconds(config: PluginConfig) -> float:
    try:
        return float(max(0, int(getattr(config, "grace_seconds", 0) or 0)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 候选项 / 方案数据结构（契约 §3.3）
# ---------------------------------------------------------------------------
@dataclass
class TorrentCandidate:
    """方向 A：因媒体消失而可能被删除的种子。"""

    torrent: TorrentItem
    linked_media: List[MediaRef] = field(default_factory=list)
    reason: str = REASON_MEDIA_DELETED
    blocked_reason: str = ""
    auto_eligible: bool = False
    delete_files: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """供 runtime 生成 status.candidates.torrents 使用的精简结构。"""
        return {
            "hash": self.torrent.hash,
            "downloader": self.torrent.downloader,
            "name": self.torrent.name,
            "reason": self.reason,
            "blocked_reason": self.blocked_reason,
            "auto_eligible": bool(self.auto_eligible),
            "delete_files": bool(self.delete_files),
            "linked_media": [ref.to_dict() for ref in self.linked_media],
        }


@dataclass
class MediaCandidate:
    """方向 B：因种子消失而可能被删除的媒体条目（也用于展示型候选）。"""

    media: MediaItem
    linked_torrents: List[TorrentRef] = field(default_factory=list)
    reason: str = REASON_TORRENT_DELETED
    blocked_reason: str = ""
    auto_eligible: bool = False
    evidence: str = "inode"

    def to_dict(self) -> Dict[str, Any]:
        """供 runtime 生成 status.candidates.media 使用的精简结构。"""
        return {
            "server": self.media.server,
            "item_id": self.media.item_id,
            "name": self.media.name,
            "reason": self.reason,
            "blocked_reason": self.blocked_reason,
            "auto_eligible": bool(self.auto_eligible),
            "evidence": self.evidence,
            "linked_torrents": [ref.to_dict() for ref in self.linked_torrents],
        }


@dataclass
class CyclePlan:
    """一轮轮询产生的处理方案。"""

    torrent_candidates: List[TorrentCandidate] = field(default_factory=list)
    media_candidates: List[MediaCandidate] = field(default_factory=list)
    actions: List[PendingAction] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------
def plan_cycle(
    config: PluginConfig,
    diff: SnapshotDiff,
    previous: Snapshot,
    current: Snapshot,
    previous_links: LinkIndex,
    current_links: LinkIndex,
    existing_action_ids: Optional[set] = None,
    now: Optional[float] = None,
) -> CyclePlan:
    """根据两次快照差异生成保守级联删除方案（契约 §3.3）。

    - ``previous`` / ``previous_links``：上一轮完整快照与索引（删除方向 A 的关联依据）；
    - ``current`` / ``current_links``：本轮完整快照与索引（“是否还有关联媒体/种子”的判定依据）；
    - ``existing_action_ids``：已存在（pending 且非 dry_run）的动作 ID 集合，用于去重；
    - ``now``：时间戳注入，便于测试。

    无论自动开关是否开启，候选都会返回供状态页/演练展示；只有对应 ``auto_*`` 开关
    开启时才生成 ``PendingAction``。``dry_run=True`` 时动作仍生成但标记 ``dry_run``。
    """
    config = config or PluginConfig()
    diff = diff or SnapshotDiff()
    previous = previous if isinstance(previous, Snapshot) else Snapshot()
    current = current if isinstance(current, Snapshot) else Snapshot()
    previous_links = previous_links if isinstance(previous_links, LinkIndex) else LinkIndex()
    current_links = current_links if isinstance(current_links, LinkIndex) else LinkIndex()
    existing_ids = {str(item) for item in (existing_action_ids or set()) if item}
    timestamp = float(time.time() if now is None else now)

    plan = CyclePlan()
    _plan_media_to_torrent(
        plan, config, diff, current, current_links, previous_links, existing_ids, timestamp
    )
    _plan_torrent_to_media(
        plan, config, diff, previous, current, previous_links, current_links, existing_ids, timestamp
    )

    blocked = sum(1 for item in plan.torrent_candidates if item.blocked_reason) + sum(
        1 for item in plan.media_candidates if item.blocked_reason
    )
    plan.summary = {
        "lost_media": len(diff.lost_media or []),
        "lost_torrents": len(diff.lost_torrents or []),
        "torrent_candidates": len(plan.torrent_candidates),
        "media_candidates": len(plan.media_candidates),
        "actions": len(plan.actions),
        "blocked": blocked,
    }
    return plan


def _append_action_notice(plan: CyclePlan, config: PluginConfig, created: int) -> None:
    """演练模式下生成动作后的统一提示（不重复刷屏）。"""
    if created and config.dry_run:
        plan.notices.append(f"演练模式：已生成 {created} 个待执行动作（不会实际删除）")


def _plan_media_to_torrent(
    plan: CyclePlan,
    config: PluginConfig,
    diff: SnapshotDiff,
    current: Snapshot,
    current_links: LinkIndex,
    previous_links: LinkIndex,
    existing_ids: set,
    now: float,
) -> None:
    """方向 A：媒体条目从媒体库消失 -> 删除关联种子（仅当所有关联媒体都消失）。"""
    lost_media: List[MediaItem] = [item for item in (diff.lost_media or []) if item is not None]
    if not lost_media:
        return

    # 批量保护：疑似媒体库刷新/批量操作，一律不自动处理
    try:
        batch_limit = int(getattr(config, "batch_limit_media", 0) or 0)
    except (TypeError, ValueError):
        batch_limit = 0
    if batch_limit > 0 and len(lost_media) >= batch_limit:
        plan.notices.append(
            f"检测到 {len(lost_media)} 个媒体条目同时消失，疑似批量操作/媒体库刷新，"
            f"已跳过自动删种，请使用扫描页确认"
        )
        return

    candidates: Dict[str, TorrentCandidate] = {}
    order: List[str] = []
    for media in lost_media:
        torrent_refs = previous_links.torrents_of(media)
        if not torrent_refs:
            # 没有关联种子的已消失媒体：仅记录候选用于展示，不产生动作
            plan.media_candidates.append(
                MediaCandidate(
                    media=media,
                    linked_torrents=[],
                    reason=REASON_MEDIA_DELETED,
                    blocked_reason=BLOCKED_NO_TORRENT_LINK,
                    auto_eligible=False,
                    evidence="inode",
                )
            )
            continue
        for torrent_ref in torrent_refs:
            torrent = current.torrents.get(torrent_ref.key)
            if torrent is None:
                # 种子本轮也已消失（由方向 B 处理），不重复生成删种动作
                continue
            candidate = candidates.get(torrent_ref.key)
            if candidate is None:
                candidate = TorrentCandidate(torrent=torrent, linked_media=[], reason=REASON_MEDIA_DELETED)
                candidates[torrent_ref.key] = candidate
                order.append(torrent_ref.key)
                blocked = _blocked_for_torrent_candidate(config, torrent, current_links)
                action_id = action_id_for("torrents", [torrent_ref.key])
                if not blocked and action_id in existing_ids:
                    blocked = BLOCKED_ALREADY_PENDING
                candidate.blocked_reason = blocked
                candidate.auto_eligible = not blocked
                # 删除下载文件的安全性：与其它现存种子共享 inode / 仍有媒体引用 -> 保留文件
                safe_map = safe_delete_files_map([torrent_ref.key], current_links)
                candidate.delete_files = bool(
                    getattr(config, "delete_torrent_files", True)
                    and getattr(config, "auto_delete_files", True)
                    and safe_map.get(torrent_ref.key, False)
                )
            # 记录触发本候选的“已消失媒体”引用（去重）
            media_ref = MediaRef(server=media.server, item_id=media.item_id)
            if media_ref not in candidate.linked_media:
                candidate.linked_media.append(media_ref)

    plan.torrent_candidates.extend(candidates[key] for key in order)

    eligible = [item for item in candidates.values() if item.auto_eligible]
    if not getattr(config, "auto_media_to_torrent", False):
        if eligible:
            plan.notices.append(
                f"检测到 {len(lost_media)} 个媒体条目消失、{len(eligible)} 个关联种子可处理；"
                f"自动删种开关未开启，仅展示候选（演练）"
            )
        return

    grace = _grace_seconds(config)
    for key in order:
        candidate = candidates[key]
        if not candidate.auto_eligible or candidate.blocked_reason:
            continue
        torrent = candidate.torrent
        plan.actions.append(
            PendingAction(
                action_id=action_id_for("torrents", [key]),
                kind="delete_torrents",
                created_at=now,
                due_at=now + grace,
                reason="媒体条目已从 Jellyfin 删除",
                payload={
                    "torrents": [
                        {
                            "downloader": torrent.downloader,
                            "hash": torrent.hash,
                            "name": torrent.name,
                            "size": int(torrent.size or 0),
                            "delete_files": bool(candidate.delete_files),
                        }
                    ]
                },
                dry_run=bool(config.dry_run),
                status="pending",
            )
        )
    _append_action_notice(plan, config, len(plan.actions))


def _blocked_for_torrent_candidate(
    config: PluginConfig, torrent: TorrentItem, current_links: LinkIndex
) -> str:
    """方向 A 的种子候选阻止原因（按契约顺序检查）。"""
    ignored = is_ignored(torrent, config)
    if ignored:
        return ignored
    # v1 配置无 only_completed_torrents 字段，按需兼容未来版本
    if getattr(config, "only_completed_torrents", False) and not torrent.completed:
        return BLOCKED_UNFINISHED
    if current_links.media_of(torrent):
        # 还有其它关联媒体存在：绝不能删种
        return BLOCKED_TORRENT_STILL_HAS_MEDIA
    return ""


def _plan_torrent_to_media(
    plan: CyclePlan,
    config: PluginConfig,
    diff: SnapshotDiff,
    previous: Snapshot,
    current: Snapshot,
    previous_links: LinkIndex,
    current_links: LinkIndex,
    existing_ids: set,
    now: float,
) -> None:
    """方向 B：种子从下载器消失 -> 删除关联媒体（仅当无其它现存种子引用同一文件）。"""
    lost_torrents: List[TorrentItem] = [
        item for item in (diff.lost_torrents or []) if item is not None
    ]
    if not lost_torrents:
        return

    # 批量保护：疑似自动清理/批量删种，一律不自动处理
    try:
        batch_limit = int(getattr(config, "batch_limit_torrents", 0) or 0)
    except (TypeError, ValueError):
        batch_limit = 0
    if batch_limit > 0 and len(lost_torrents) >= batch_limit:
        plan.notices.append(
            f"检测到 {len(lost_torrents)} 个下载任务同时消失，疑似批量删除/自动清理，"
            f"已跳过自动删媒体，请使用扫描页确认"
        )
        return

    auto_enabled = bool(getattr(config, "auto_torrent_to_media", False))
    grace = _grace_seconds(config)
    eligible_media_count = 0
    # 媒体 key -> 候选：同一媒体只记录一个候选/一个动作（cross-seed 多个种子同时消失时去重）
    handled: Dict[str, MediaCandidate] = {}

    for torrent in lost_torrents:
        torrent_ref = TorrentRef(downloader=torrent.downloader, hash=torrent.hash)
        media_refs = previous_links.media_of(torrent)
        ignored = is_ignored(torrent, config)
        new_candidates: List[MediaCandidate] = []
        eligible_items: List[MediaItem] = []

        for ref in media_refs:
            media = current.media.get(ref.key)
            if media is None:
                # 关联媒体也已从媒体库消失：仅记录展示型候选，不生成动作
                previous_media = previous.media.get(ref.key)
                if previous_media is None:
                    continue
                existing = handled.get(ref.key)
                if existing is not None:
                    if torrent_ref not in existing.linked_torrents:
                        existing.linked_torrents.append(torrent_ref)
                    continue
                candidate = MediaCandidate(
                    media=previous_media,
                    linked_torrents=[torrent_ref],
                    reason=REASON_TORRENT_DELETED,
                    blocked_reason=BLOCKED_MEDIA_ALREADY_GONE,
                    auto_eligible=False,
                    evidence=_evidence_of(previous_links, previous_media, torrent),
                )
                handled[ref.key] = candidate
                new_candidates.append(candidate)
                continue
            existing = handled.get(ref.key)
            if existing is not None:
                # 该媒体已由本轮其它已删种子处理，只补充关联信息
                if torrent_ref not in existing.linked_torrents:
                    existing.linked_torrents.append(torrent_ref)
                continue
            blocked = ""
            if ignored:
                blocked = ignored
            elif not media.has_inode:
                # 无法校验是否还有其它种子引用该文件：保守不自动删
                blocked = BLOCKED_NO_INODE
            elif current_links.current_torrents_of_inode(media.inode_key):
                # 还有其它现存种子引用同一 inode：绝不能删媒体
                blocked = BLOCKED_TORRENT_STILL_SHARED
            candidate = MediaCandidate(
                media=media,
                linked_torrents=[torrent_ref],
                reason=REASON_TORRENT_DELETED,
                blocked_reason=blocked,
                auto_eligible=not blocked,
                evidence=_evidence_of(previous_links, media, torrent),
            )
            handled[ref.key] = candidate
            new_candidates.append(candidate)
            if not blocked:
                eligible_items.append(media)

        action_id = (
            action_id_for("media", [media.key for media in eligible_items])
            if eligible_items
            else ""
        )
        if action_id and action_id in existing_ids:
            for candidate in new_candidates:
                if candidate.auto_eligible:
                    candidate.blocked_reason = BLOCKED_ALREADY_PENDING
                    candidate.auto_eligible = False
            eligible_items = []

        plan.media_candidates.extend(new_candidates)
        eligible_media_count += len(eligible_items)
        if not eligible_items:
            continue

        if auto_enabled:
            plan.actions.append(
                PendingAction(
                    action_id=action_id,
                    kind="delete_media",
                    created_at=now,
                    due_at=now + grace,
                    reason="下载任务已从下载器删除",
                    payload={"items": [_media_payload(media) for media in eligible_items]},
                    dry_run=bool(config.dry_run),
                    status="pending",
                )
            )

    if not auto_enabled:
        if eligible_media_count:
            plan.notices.append(
                f"检测到 {len(lost_torrents)} 个下载任务消失、{eligible_media_count} 个媒体条目可处理；"
                f"自动删媒体开关未开启，仅展示候选（演练）"
            )
        return
    _append_action_notice(plan, config, len(plan.actions))


def _media_payload(media: MediaItem) -> Dict[str, Any]:
    """delete_media 动作 payload 中的单个媒体条目（契约 §3.3）。"""
    return {
        "server": media.server,
        "item_id": media.item_id,
        "path": media.path,
        "name": media.name,
        "item_type": media.item_type,
        "series_name": media.series_name,
        "season": int(media.season or 0),
        "episode": int(media.episode or 0),
        "inode": int(media.inode or 0),
        "dev": int(media.dev or 0),
    }


# ---------------------------------------------------------------------------
# 删除文件安全性
# ---------------------------------------------------------------------------
def safe_delete_files_map(torrent_keys: Iterable[str], links: LinkIndex) -> Dict[str, bool]:
    """对每个待删种子判断能否安全删除其下载文件（契约 §3.3）。

    仅当同时满足以下条件才返回 ``True``：

    - 该种子在 ``links.torrent_inodes`` 中登记的视频 inode 列表非空；
    - 每个 inode 在 ``links.inode_torrents`` 中的所有引用都在本次待删集合内
      （即没有其它现存种子共享这些文件）；
    - 每个 inode 在 ``links.inode_media`` 中没有当前媒体库引用。
    """
    if links is None:
        links = LinkIndex()
    keys = [str(key) for key in (torrent_keys or []) if str(key)]
    key_set = set(keys)
    result: Dict[str, bool] = {}
    for key in keys:
        result[key] = _torrent_files_safe(key, links, key_set)
    return result


def _torrent_files_safe(key: str, links: LinkIndex, key_set: set) -> bool:
    inode_texts = links.inodes_of_torrent(key)
    if not inode_texts:
        # 没有视频文件索引（路径不可见/无视频）时无法证明安全
        return False
    for inode_text in inode_texts:
        dev, inode = parse_inode_key(inode_text)
        if not dev or not inode:
            return False
        sharers = links.inode_torrents.get(inode_text) or []
        sharer_keys = {ref.key for ref in sharers}
        if not sharer_keys:
            # 索引不一致：无法确认该 inode 的引用关系，保守拒绝
            return False
        if not sharer_keys.issubset(key_set):
            return False
        if links.inode_media.get(inode_text):
            return False
    return True


# ---------------------------------------------------------------------------
# 执行前复核
# ---------------------------------------------------------------------------
def recheck_torrent_action(
    action: PendingAction,
    current: Snapshot,
    current_links: LinkIndex,
    config: PluginConfig,
) -> Tuple[bool, str]:
    """执行 :class:`PendingAction` 前的复核（删种子方向）。

    必须同时满足：种子仍存在于 ``current.torrents``；
    该种子的 ``current_links.media_of(...)`` 为空（所有关联媒体确已消失）；
    未命中忽略类目/标签。返回 ``(True, "")`` 或 ``(False, 原因)``。
    """
    if action is None:
        return False, "动作为空"
    if action.kind != "delete_torrents":
        return False, f"动作类型不匹配：{action.kind}"
    current = current if isinstance(current, Snapshot) else Snapshot()
    current_links = current_links if isinstance(current_links, LinkIndex) else LinkIndex()
    config = config or PluginConfig()
    entries = (action.payload or {}).get("torrents") or []
    if not isinstance(entries, list) or not entries:
        return False, "动作缺少待删种子"
    for entry in entries:
        if not isinstance(entry, dict):
            return False, "动作目标格式错误"
        downloader = str(entry.get("downloader") or "").strip()
        torrent_hash = str(entry.get("hash") or "").strip().lower()
        if not downloader or not torrent_hash:
            return False, "动作目标缺少下载器或 hash"
        key = torrent_key(downloader, torrent_hash)
        torrent = current.torrents.get(key)
        if torrent is None:
            return False, f"种子已不存在：{key}"
        ignored = is_ignored(torrent, config)
        if ignored:
            return False, f"种子命中忽略规则（{ignored}）：{key}"
        media_refs = current_links.media_of(torrent)
        if media_refs:
            return False, f"种子仍有 {len(media_refs)} 个现存媒体关联，已跳过：{key}"
    return True, ""


def recheck_media_action(
    action: PendingAction,
    current: Snapshot,
    current_links: LinkIndex,
    config: PluginConfig,
) -> Tuple[bool, str]:
    """执行 :class:`PendingAction` 前的复核（删媒体方向）。

    必须同时满足：媒体条目仍在 ``current.media`` 中；
    该媒体 inode 在 ``current_links.inode_torrents`` 中无任何现存引用；
    媒体路径在 ``config.media_delete_roots`` 内。返回 ``(True, "")`` 或 ``(False, 原因)``。
    """
    if action is None:
        return False, "动作为空"
    if action.kind != "delete_media":
        return False, f"动作类型不匹配：{action.kind}"
    current = current if isinstance(current, Snapshot) else Snapshot()
    current_links = current_links if isinstance(current_links, LinkIndex) else LinkIndex()
    config = config or PluginConfig()
    entries = (action.payload or {}).get("items") or []
    if not isinstance(entries, list) or not entries:
        return False, "动作缺少待删媒体"
    for entry in entries:
        if not isinstance(entry, dict):
            return False, "动作目标格式错误"
        server = str(entry.get("server") or "").strip()
        item_id = str(entry.get("item_id") or "").strip()
        if not server or not item_id:
            return False, "动作目标缺少媒体服务器或 item_id"
        key = media_key(server, item_id)
        media = current.media.get(key)
        if media is None:
            return False, f"媒体条目已不存在：{key}"
        if not media.has_inode:
            return False, f"媒体条目无法校验 inode（文件可能已变更），已跳过：{key}"
        refs = current_links.current_torrents_of_inode(media.inode_key)
        if refs:
            return False, f"媒体文件仍被 {len(refs)} 个现存种子引用，已跳过：{key}"
        if not _path_under_roots(media.path, getattr(config, "media_delete_roots", [])):
            return False, f"媒体路径不在允许删除的目录内：{media.path}"
        # 与手工删除路径保持一致的安全阀：媒体路径不能落在下载目录内，
        # 也不能是任何根目录本身/父目录，避免路径映射错误时误删下载文件。
        try:
            from .cleanup import is_safe_delete_target
        except ImportError:  # pragma: no cover - 独立脚本/测试
            from cleanup import is_safe_delete_target
        if not is_safe_delete_target(media.path, config):
            return False, f"媒体路径未通过删除安全阀：{media.path}"
    return True, ""


# ---------------------------------------------------------------------------
# 一次性扫描结果（契约 §5.2 的 result 结构）
# ---------------------------------------------------------------------------
SKIPPED_DEFAULTS: Dict[str, int] = {
    "downloading": 0,
    "no_video": 0,
    "ignored": 0,
    "missing_path": 0,
    "unsupported": 0,
}

STATS_DEFAULTS: Dict[str, int] = {
    "media_total": 0,
    "torrent_total": 0,
    "link_pairs": 0,
    "torrents_walked": 0,
    "torrents_unsupported": 0,
    "torrents_missing_path": 0,
    "video_files": 0,
    "walk_errors": 0,
}


@dataclass
class ScanResult:
    """一次性扫描结果（``build_scan`` 返回其 ``to_dict()``）。"""

    generated_at: float = 0.0
    duration: float = 0.0
    stats: Dict[str, int] = field(default_factory=lambda: dict(STATS_DEFAULTS))
    media_without_torrent: List[Dict[str, Any]] = field(default_factory=list)
    media_groups: List[Dict[str, Any]] = field(default_factory=list)
    torrents_without_media: List[Dict[str, Any]] = field(default_factory=list)
    torrent_groups: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=lambda: dict(SKIPPED_DEFAULTS))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": float(self.generated_at),
            "duration": float(self.duration),
            "stats": dict(self.stats),
            "media_without_torrent": list(self.media_without_torrent),
            "media_groups": list(self.media_groups),
            "torrents_without_media": list(self.torrents_without_media),
            "torrent_groups": list(self.torrent_groups),
            "skipped": dict(self.skipped),
        }


def build_scan(
    config: PluginConfig,
    snapshot: Snapshot,
    links: LinkIndex,
    history_rows: Optional[Iterable[Tuple[str, str, str]]] = None,
    now: Optional[float] = None,
    stats: Optional[Any] = None,
    duration: float = 0.0,
) -> Dict[str, Any]:
    """生成一次性扫描结果（JSON 可序列化，结构见契约 §5.2 的 ``result``）。

    只读取 ``snapshot`` / ``links``，不会修改它们。

    :param history_rows: ``(downloader, torrent_hash, media_path)`` 整理记录，
        用于给“无种子关联”的媒体展示 ``history_hashes``（不影响删除安全判断）。
    :param stats: 可选，``LinkBuildStats`` 实例或其 ``to_dict()`` 结果；
        传入时用真实的遍历统计覆盖估算值（runtime 建议传入）。
    :param duration: 扫描耗时（秒），由 runtime 测量后填入。
    """
    config = config or PluginConfig()
    snapshot = snapshot if isinstance(snapshot, Snapshot) else Snapshot()
    links = links if isinstance(links, LinkIndex) else LinkIndex()
    generated_at = float(time.time() if now is None else now)

    # 历史记录索引：规范化路径 -> hash 列表（用于展示）
    history_map: Dict[str, List[str]] = {}
    for row in history_rows or []:
        if row is None:
            continue
        try:
            _downloader, torrent_hash, media_path = row
        except (TypeError, ValueError):
            continue
        if not torrent_hash or not media_path:
            continue
        path_key = _normalize_media_path(media_path)
        if not path_key:
            continue
        hashes = history_map.setdefault(path_key, [])
        hashed = str(torrent_hash).lower()
        if hashed and hashed not in hashes:
            hashes.append(hashed)

    # 无种子关联的媒体（路径不可见的条目也列出，便于人工判断；安全删除由 recheck 兜底）
    media_without_torrent: List[Dict[str, Any]] = []
    for media in snapshot.media.values():
        if media is None:
            continue
        if links.torrents_of(media):
            continue
        path_key = _normalize_media_path(media.path)
        media_without_torrent.append(
            {
                "id": media.key,
                "server": media.server,
                "item_id": media.item_id,
                "name": media.name,
                "display_title": media.display_title,
                "item_type": media.item_type,
                "series_name": media.series_name,
                "season": int(media.season or 0),
                "episode": int(media.episode or 0),
                "path": media.path,
                "folder": media.folder,
                "size": int(media.size or 0),
                "inode": int(media.inode or 0),
                "dev": int(media.dev or 0),
                "history_hashes": list(history_map.get(path_key) or []),
                "reason": "no_torrent_link",
            }
        )
    media_without_torrent.sort(key=lambda row: (row["folder"], row["path"], row["id"]))

    # 无媒体关联的种子 / 各类跳过统计
    skipped = dict(SKIPPED_DEFAULTS)
    torrents_without_media: List[Dict[str, Any]] = []
    for key in sorted(snapshot.torrents.keys()):
        torrent = snapshot.torrents.get(key)
        if torrent is None:
            continue
        ignored_reason = is_ignored(torrent, config) or ""
        if torrent.unsupported_path:
            skipped["unsupported"] += 1
            continue
        content_path = str(torrent.content_path or "").strip()
        if not content_path:
            skipped["missing_path"] += 1
            continue
        if not torrent.completed and not getattr(config, "scan_include_unfinished", False):
            skipped["downloading"] += 1
            continue
        inodes = links.inodes_of_torrent(key)
        video_files = len(inodes) if inodes else int(torrent.video_files or 0)
        if not torrent.path_exists:
            # 内容已缺失：列出便于清理无效任务，但禁止删除文件（无法校验）
            torrents_without_media.append(
                _torrent_row(
                    torrent,
                    reason="missing_content",
                    ignored_reason=ignored_reason,
                    video_files=video_files,
                    shared_torrents=0,
                    safe_to_delete_files=False,
                )
            )
            if ignored_reason:
                skipped["ignored"] += 1
            continue
        if not inodes:
            skipped["no_video"] += 1
            continue
        if links.media_of(key):
            # 正常关联媒体，无需展示
            continue
        shared_torrents = _shared_torrent_count(key, inodes, links)
        safe_map = safe_delete_files_map([key], links)
        reason = "no_media_link" if torrent.completed else "downloading"
        torrents_without_media.append(
            _torrent_row(
                torrent,
                reason=reason,
                ignored_reason=ignored_reason,
                video_files=video_files,
                shared_torrents=shared_torrents,
                safe_to_delete_files=bool(safe_map.get(key, False)),
            )
        )
        if ignored_reason:
            skipped["ignored"] += 1
    torrents_without_media.sort(key=lambda row: (row["downloader"], row["name"], row["id"]))

    result = ScanResult(
        generated_at=generated_at,
        duration=float(duration or 0.0),
        stats=_compose_stats(snapshot, links, stats),
        media_without_torrent=media_without_torrent,
        media_groups=_group_media_rows(media_without_torrent),
        torrents_without_media=torrents_without_media,
        torrent_groups=_group_torrent_rows(torrents_without_media),
        skipped=skipped,
    )
    return result.to_dict()


def _normalize_media_path(path: Any) -> str:
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        return os.path.normpath(text)
    except (TypeError, ValueError):
        return text


def _compose_stats(snapshot: Snapshot, links: LinkIndex, build_stats: Optional[Any]) -> Dict[str, int]:
    """合并统计信息：优先使用真实 LinkBuildStats，否则从快照/索引估算。"""
    stats = dict(STATS_DEFAULTS)
    stats["media_total"] = len(snapshot.media)
    stats["torrent_total"] = len(snapshot.torrents)
    stats["link_pairs"] = int(links.link_count)
    stats["video_files"] = sum(len(inodes) for inodes in links.torrent_inodes.values())
    for torrent in snapshot.torrents.values():
        if torrent.unsupported_path:
            stats["torrents_unsupported"] += 1
        elif not str(torrent.content_path or "").strip():
            stats["torrents_missing_path"] += 1
        else:
            stats["torrents_walked"] += 1
    override = _stats_override(build_stats)
    for key, value in override.items():
        if key in stats:
            stats[key] = int(value)
    return stats


def _stats_override(build_stats: Optional[Any]) -> Dict[str, int]:
    """把 LinkBuildStats / dict 归一化为契约 stats 键。"""
    if build_stats is None:
        return {}
    raw: Dict[str, Any] = {}
    if isinstance(build_stats, dict):
        raw = dict(build_stats)
    elif hasattr(build_stats, "to_dict") and callable(build_stats.to_dict):
        try:
            converted = build_stats.to_dict()
            if isinstance(converted, dict):
                raw = dict(converted)
        except Exception:  # noqa: BLE001 - 统计信息不应影响扫描
            logger.debug("解析 LinkBuildStats 失败", exc_info=True)
    else:
        for name in (
            "media_total",
            "torrents_total",
            "linked_pairs",
            "torrents_walked",
            "torrents_unsupported",
            "torrents_missing_path",
            "video_files",
            "walk_errors",
        ):
            value = getattr(build_stats, name, None)
            if value is not None:
                raw[name] = value
    mapping = {"torrents_total": "torrent_total", "linked_pairs": "link_pairs"}
    result: Dict[str, int] = {}
    for name, value in raw.items():
        if value is None:
            continue
        stat_key = mapping.get(str(name), str(name))
        if stat_key not in STATS_DEFAULTS:
            continue
        try:
            result[stat_key] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _shared_torrent_count(torrent_key_text: str, inodes: List[str], links: LinkIndex) -> int:
    """与其它现存种子共享文件的数量。"""
    sharers: set = set()
    for inode_text in inodes:
        for ref in links.inode_torrents.get(inode_text) or []:
            if ref.key != torrent_key_text:
                sharers.add(ref.key)
    return len(sharers)


def _torrent_row(
    torrent: TorrentItem,
    *,
    reason: str,
    ignored_reason: str,
    video_files: int,
    shared_torrents: int,
    safe_to_delete_files: bool,
) -> Dict[str, Any]:
    return {
        "id": torrent.key,
        "downloader": torrent.downloader,
        "hash": torrent.hash,
        "name": torrent.name,
        "category": torrent.category,
        "tags": _torrent_tags(torrent),
        "state": torrent.state,
        "progress": float(torrent.progress or 0.0),
        "size": int(torrent.size or 0),
        "save_path": torrent.save_path,
        "content_path": torrent.content_path,
        "added_on": int(torrent.added_on or 0),
        "completion_on": int(torrent.completion_on or 0),
        "completed": bool(torrent.completed),
        "video_files": int(video_files or 0),
        "path_exists": bool(torrent.path_exists),
        "reason": reason,
        "ignored_reason": ignored_reason or "",
        "shared_torrents": int(shared_torrents or 0),
        "safe_to_delete_files": bool(safe_to_delete_files),
    }


def _media_group_title(
    item_type: str, series_name: str, season: int, folder: str, name: str
) -> str:
    if item_type == "Episode" and series_name:
        return f"{series_name} S{int(season or 0):02d}"
    if series_name:
        return series_name
    if folder:
        base = os.path.basename(folder.rstrip("/")) or folder
        return base
    return name or folder or "未命名"


def _group_media_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        folder = row.get("folder") or ""
        group_id = f"folder:{folder}"
        group = groups.get(group_id)
        if group is None:
            group = {
                "group_id": group_id,
                "title": _media_group_title(
                    str(row.get("item_type") or ""),
                    str(row.get("series_name") or ""),
                    int(row.get("season") or 0),
                    folder,
                    str(row.get("name") or ""),
                ),
                "item_type": str(row.get("item_type") or ""),
                "folder": folder,
                "count": 0,
                "size": 0,
                "item_ids": [],
            }
            groups[group_id] = group
        group["count"] += 1
        group["size"] += int(row.get("size") or 0)
        group["item_ids"].append(row["id"])
    return [groups[key] for key in sorted(groups.keys())]


def _group_torrent_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        category = str(row.get("category") or "").strip()
        group_id = f"category:{category}"
        group = groups.get(group_id)
        if group is None:
            group = {
                "group_id": group_id,
                "title": category or "未分类",
                "count": 0,
                "size": 0,
            }
            groups[group_id] = group
        group["count"] += 1
        group["size"] += int(row.get("size") or 0)
    return [groups[key] for key in sorted(groups.keys())]
