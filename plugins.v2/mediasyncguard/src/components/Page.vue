<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  asArray,
  asNumber,
  asObject,
  errorMessage,
  formatCount,
  formatDuration,
  formatRemaining,
  formatSize,
  formatTime,
  unwrapResponse,
} from '../provider'

/**
 * 媒体同步守护管理页。
 * 所有数据均来自插件 API（不直接访问 Jellyfin / qBittorrent）：
 *  - GET  plugin/{id}/status        插件状态 / 最近轮询 / 候选项
 *  - GET  plugin/{id}/scan/result   扫描状态与结果
 *  - POST plugin/{id}/scan          启动后台扫描
 *  - POST plugin/{id}/apply         执行选中的删除
 *  - GET  plugin/{id}/pending       待执行动作与历史
 *  - POST plugin/{id}/pending/cancel 取消单个 / 全部待执行动作
 */
const props = defineProps({
  api: { type: Object, default: () => ({}) },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'MediaSyncGuard' },
  nativeSubscribe: { type: Function, default: null },
  showSwitch: { type: Boolean, default: true },
})

const emit = defineEmits(['close', 'switch'])

const ITEM_TYPE_LABELS = {
  Movie: '电影',
  Episode: '剧集',
  Series: '剧集',
  Season: '季',
}

const MEDIA_REASON_LABELS = { no_torrent_link: '无种子关联' }

const TORRENT_REASON_LABELS = {
  no_media_link: '无媒体关联',
  missing_content: '路径缺失',
  downloading: '下载中',
}

const BLOCKED_REASON_LABELS = {
  ignored_category: '忽略类目',
  ignored_tag: '忽略标签',
  torrent_still_has_media: '种子仍有媒体',
  torrent_still_shared: '仍有种子共享',
  no_inode: '无法校验 inode',
  no_torrent_link: '无种子关联',
  already_pending: '已在待办',
  unfinished: '未完成',
}

const IGNORED_REASON_LABELS = {
  ignored_category: '忽略类目',
  ignored_tag: '忽略标签',
}

const PENDING_KIND_LABELS = {
  delete_torrents: '删除种子',
  delete_media: '删除媒体',
}

const PENDING_STATUS_LABELS = {
  pending: '待执行',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
  cancelled: '已取消',
}

const SKIPPED_LABELS = {
  downloading: '下载中',
  no_video: '无视频文件',
  ignored: '忽略类目/标签',
  missing_path: '路径不可见',
  unsupported: '路径不支持',
}

const basePath = computed(() => `plugin/${props.pluginId}`)

// ---------------------------------------------------------------- 数据状态
const status = ref({})
const statusLoaded = ref(false)
const scanState = ref({})
const pendingData = ref({ pending: [], history: [] })

const errors = reactive({ status: '', scan: '', pending: '', action: '' })
const notice = ref(null)

const autoRefresh = ref(true)
const refreshing = ref(false)
const scanning = ref(false)
const applying = ref(false)
const cancelling = ref('')
const nowSeconds = ref(Math.floor(Date.now() / 1000))

let refreshTimer = null
let clockTimer = null
let disposed = false

// ---------------------------------------------------------------- 状态派生
const safeStatus = computed(() => asObject(status.value))
const lastPoll = computed(() => asObject(safeStatus.value.last_poll))
const pendingInfo = computed(() => asObject(safeStatus.value.pending))
const scanInfo = computed(() => asObject(safeStatus.value.scan))
const candidates = computed(() => {
  const raw = asObject(safeStatus.value.candidates)
  return {
    torrents: asArray(raw.torrents),
    media: asArray(raw.media),
  }
})
const statusWarnings = computed(() => asArray(safeStatus.value.warnings).filter(Boolean))
const pollErrors = computed(() => asArray(lastPoll.value.errors).filter(Boolean))
const pollWarnings = computed(() => asArray(lastPoll.value.warnings).filter(Boolean))
const hasPolled = computed(() => asNumber(lastPoll.value.at) > 0)

const enabled = computed(() => Boolean(safeStatus.value.enabled))
const dryRun = computed(() => safeStatus.value.dry_run !== false)
const autoMode = computed(() => {
  const auto = asObject(safeStatus.value.auto)
  return Boolean(auto.media_to_torrent || auto.torrent_to_media)
})

const scanPayload = computed(() => asObject(scanState.value))
const scanResult = computed(() => (scanPayload.value.result ? asObject(scanPayload.value.result) : null))
const scanRunning = computed(() => Boolean(scanPayload.value.running || scanInfo.value.running))
const scanProgress = computed(() => String(scanPayload.value.progress || ''))
const scanError = computed(() => String(scanPayload.value.error || ''))
const scanFinishedAt = computed(() => asNumber(scanPayload.value.finished_at, asNumber(scanResult.value?.generated_at)))
const scanGeneratedAt = computed(() => asNumber(scanResult.value?.generated_at))
const scanDuration = computed(() => asNumber(scanPayload.value.duration, asNumber(scanResult.value?.duration)))

const summaryCards = computed(() => {
  const poll = lastPoll.value
  const stats = asObject(scanResult.value?.stats)
  return [
    {
      key: 'media',
      label: '媒体条目',
      value: asNumber(poll.media_total, asNumber(stats.media_total)),
      icon: 'mdi-filmstrip',
      color: 'primary',
    },
    {
      key: 'torrents',
      label: '种子',
      value: asNumber(poll.torrent_total, asNumber(stats.torrent_total)),
      icon: 'mdi-download',
      color: 'info',
    },
    {
      key: 'links',
      label: '硬链接关联',
      value: asNumber(poll.links, asNumber(stats.link_pairs)),
      icon: 'mdi-link-variant',
      color: 'teal',
    },
    {
      key: 'pending',
      label: '待执行动作',
      value: asNumber(pendingInfo.value.count),
      icon: 'mdi-clock-alert-outline',
      color: asNumber(pendingInfo.value.count) > 0 ? 'warning' : 'success',
    },
  ]
})

const scanStatsChips = computed(() => {
  const stats = asObject(scanResult.value?.stats)
  return [
    { key: 'media_total', label: '媒体总数', value: asNumber(stats.media_total) },
    { key: 'torrent_total', label: '种子总数', value: asNumber(stats.torrent_total) },
    { key: 'link_pairs', label: '硬链接关联', value: asNumber(stats.link_pairs) },
    { key: 'torrents_walked', label: '遍历种子', value: asNumber(stats.torrents_walked) },
    { key: 'video_files', label: '视频文件', value: asNumber(stats.video_files) },
    { key: 'torrents_missing_path', label: '路径缺失', value: asNumber(stats.torrents_missing_path) },
    { key: 'torrents_unsupported', label: '路径不支持', value: asNumber(stats.torrents_unsupported) },
    { key: 'walk_errors', label: '遍历错误', value: asNumber(stats.walk_errors) },
  ]
})

const skippedChips = computed(() => {
  const skipped = asObject(scanResult.value?.skipped)
  return Object.entries(SKIPPED_LABELS).map(([key, label]) => ({
    key,
    label,
    value: asNumber(skipped[key]),
  }))
})

// ---------------------------------------------------------------- 列表数据
const mediaItems = computed(() => asArray(scanResult.value?.media_without_torrent))
const mediaGroups = computed(() => asArray(scanResult.value?.media_groups))
const torrentItems = computed(() => asArray(scanResult.value?.torrents_without_media))
const torrentGroups = computed(() => asArray(scanResult.value?.torrent_groups))

// ---------------------------------------------------------------- 待执行动作
const pendingList = computed(() => asArray(pendingData.value.pending))
const pendingHistory = computed(() => asArray(pendingData.value.history))
const dryRunPendingCount = computed(() => pendingList.value.filter(item => item.dry_run).length)
const candidateTorrents = computed(() => candidates.value.torrents)
const candidateMedia = computed(() => candidates.value.media)
const candidateCount = computed(() => candidateTorrents.value.length + candidateMedia.value.length)
const nextDueAt = computed(() => asNumber(pendingInfo.value.next_due_at))
const showCandidates = ref(false)
const showHistory = ref(false)

// ---------------------------------------------------------------- 筛选 / 选择
const activeTab = ref('media')
const mediaSearchText = ref('')
const mediaTypeFilter = ref('all')
const torrentSearchText = ref('')
const torrentCategoryFilter = ref('all')

const selectedMedia = ref(new Set())
const selectedTorrents = ref(new Set())

const normalize = value => String(value ?? '').toLowerCase()

const mediaTypeOptions = computed(() => {
  const types = []
  for (const item of mediaItems.value) {
    const type = String(item?.item_type || '')
    if (type && !types.includes(type)) types.push(type)
  }
  return [
    { title: '全部类型', value: 'all' },
    ...types.map(type => ({ title: ITEM_TYPE_LABELS[type] || type, value: type })),
  ]
})

const torrentCategoryOptions = computed(() => {
  const categories = []
  for (const item of torrentItems.value) {
    const category = String(item?.category || '')
    if (category && !categories.includes(category)) categories.push(category)
  }
  return [
    { title: '全部分类', value: 'all' },
    ...categories.map(category => ({ title: category, value: category })),
  ]
})

const filteredMedia = computed(() => {
  const text = normalize(mediaSearchText.value.trim())
  const type = mediaTypeFilter.value
  return mediaItems.value.filter(item => {
    if (type !== 'all' && String(item?.item_type || '') !== type) return false
    if (!text) return true
    return [
      item?.display_title,
      item?.name,
      item?.series_name,
      item?.path,
      item?.folder,
      item?.item_id,
      item?.server,
    ].some(value => normalize(value).includes(text))
  })
})

const filteredTorrents = computed(() => {
  const text = normalize(torrentSearchText.value.trim())
  const category = torrentCategoryFilter.value
  return torrentItems.value.filter(item => {
    if (category !== 'all' && String(item?.category || '') !== category) return false
    if (!text) return true
    return [
      item?.name,
      item?.hash,
      item?.downloader,
      item?.category,
      item?.state,
      item?.content_path,
      item?.save_path,
      asArray(item?.tags).join(' '),
    ].some(value => normalize(value).includes(text))
  })
})

const selectableMediaIds = computed(() => filteredMedia.value.map(item => item?.id).filter(Boolean))
const selectableTorrentIds = computed(() => filteredTorrents.value
  .filter(item => !item?.ignored_reason)
  .map(item => item?.id)
  .filter(Boolean))

const allMediaSelected = computed(() => (
  selectableMediaIds.value.length > 0
  && selectableMediaIds.value.every(id => selectedMedia.value.has(id))
))
const someMediaSelected = computed(() => (
  !allMediaSelected.value && selectableMediaIds.value.some(id => selectedMedia.value.has(id))
))
const allTorrentsSelected = computed(() => (
  selectableTorrentIds.value.length > 0
  && selectableTorrentIds.value.every(id => selectedTorrents.value.has(id))
))
const someTorrentsSelected = computed(() => (
  !allTorrentsSelected.value && selectableTorrentIds.value.some(id => selectedTorrents.value.has(id))
))

const selectedMediaItems = computed(() => mediaItems.value.filter(item => selectedMedia.value.has(item?.id)))
const selectedTorrentItems = computed(() => torrentItems.value.filter(item => selectedTorrents.value.has(item?.id)))
const selectedMediaCount = computed(() => selectedMediaItems.value.length)
const selectedTorrentCount = computed(() => selectedTorrentItems.value.length)
const selectedMediaSize = computed(() => selectedMediaItems.value
  .reduce((total, item) => total + asNumber(item?.size), 0))
const selectedTorrentSize = computed(() => selectedTorrentItems.value
  .reduce((total, item) => total + asNumber(item?.size), 0))

// 扫描结果刷新后清理已不存在的选择，避免提交过期 id
function pruneSelection(items, selected) {
  const available = new Set(asArray(items).map(item => item?.id).filter(Boolean))
  const next = new Set([...selected.value].filter(id => available.has(id)))
  if (next.size !== selected.value.size) selected.value = next
}

watch(mediaItems, items => pruneSelection(items, selectedMedia))
watch(torrentItems, items => pruneSelection(items, selectedTorrents))

function toggleMediaSelection(id, value) {
  if (!id) return
  const next = new Set(selectedMedia.value)
  if (value) next.add(id)
  else next.delete(id)
  selectedMedia.value = next
}

function toggleTorrentSelection(id, value) {
  if (!id) return
  const next = new Set(selectedTorrents.value)
  if (value) next.add(id)
  else next.delete(id)
  selectedTorrents.value = next
}

function selectAllMedia(value) {
  const next = new Set(selectedMedia.value)
  for (const id of selectableMediaIds.value) {
    if (value) next.add(id)
    else next.delete(id)
  }
  selectedMedia.value = next
}

function selectAllTorrents(value) {
  const next = new Set(selectedTorrents.value)
  for (const id of selectableTorrentIds.value) {
    if (value) next.add(id)
    else next.delete(id)
  }
  selectedTorrents.value = next
}

function clearSelection(kind) {
  if (kind === 'media') selectedMedia.value = new Set()
  else selectedTorrents.value = new Set()
}

// ---------------------------------------------------------------- 分组选择
function mediaGroupIds(group) {
  const direct = asArray(group?.item_ids).filter(Boolean)
  if (direct.length) return direct
  const folder = String(group?.folder || '')
  if (!folder) return []
  return mediaItems.value.filter(item => String(item?.folder || '') === folder).map(item => item?.id).filter(Boolean)
}

function torrentGroupIds(group) {
  const raw = String(group?.group_id || '')
  const separator = raw.indexOf(':')
  const field = separator > 0 ? raw.slice(0, separator) : ''
  const value = separator > 0 ? raw.slice(separator + 1) : raw
  return torrentItems.value.filter(item => {
    if (field === 'downloader') return String(item?.downloader || '') === value
    if (field === 'category') return String(item?.category || '') === value
    return String(item?.category || '') === raw
  }).map(item => item?.id).filter(Boolean)
}

function groupSelection(ids, selected) {
  const available = asArray(ids).filter(Boolean)
  const count = available.filter(id => selected.has(id)).length
  return {
    total: available.length,
    count,
    checked: available.length > 0 && count === available.length,
    indeterminate: count > 0 && count < available.length,
  }
}

function selectGroup(kind, ids, value) {
  const available = asArray(ids).filter(Boolean)
  if (!available.length) return
  if (kind === 'media') {
    const next = new Set(selectedMedia.value)
    for (const id of available) {
      if (value) next.add(id)
      else next.delete(id)
    }
    selectedMedia.value = next
  } else {
    const next = new Set(selectedTorrents.value)
    for (const id of available) {
      if (value) next.add(id)
      else next.delete(id)
    }
    selectedTorrents.value = next
  }
}

const mediaGroupStates = computed(() => mediaGroups.value
  .map(group => {
    const ids = mediaGroupIds(group)
    return { group, ids, selection: groupSelection(ids, selectedMedia.value) }
  })
  .filter(entry => entry.ids.length > 0))

const torrentGroupStates = computed(() => torrentGroups.value
  .map(group => {
    const ids = torrentGroupIds(group)
    return { group, ids, selection: groupSelection(ids, selectedTorrents.value) }
  })
  .filter(entry => entry.ids.length > 0))

// ---------------------------------------------------------------- 展示辅助
function itemTypeLabel(type) {
  return ITEM_TYPE_LABELS[type] || type || '—'
}

function mediaReasonLabel(reason) {
  return MEDIA_REASON_LABELS[reason] || reason || ''
}

function torrentReasonLabel(reason) {
  return TORRENT_REASON_LABELS[reason] || reason || ''
}

function blockedLabel(reason) {
  return BLOCKED_REASON_LABELS[reason] || reason || ''
}

function ignoredLabel(reason) {
  return IGNORED_REASON_LABELS[reason] || reason || ''
}

function kindLabel(kind) {
  return PENDING_KIND_LABELS[kind] || kind || '动作'
}

function pendingStatusLabel(state) {
  return PENDING_STATUS_LABELS[state] || state || ''
}

function targetsOf(action) {
  const targets = asArray(action?.targets).filter(Boolean)
  if (targets.length) return targets
  const payload = asObject(action?.payload)
  if (action?.kind === 'delete_torrents') {
    return asArray(payload.torrents)
      .map(item => [item?.downloader, item?.hash].filter(Boolean).join('|'))
      .filter(Boolean)
  }
  return asArray(payload.items)
    .map(item => item?.item_id || item?.path || '')
    .filter(Boolean)
}

function candidateName(item) {
  return item?.name || item?.hash || item?.item_id || item?.path || '—'
}

function progressPercent(value) {
  const percent = Math.round(asNumber(value) * 100)
  return Math.max(0, Math.min(100, percent))
}

// ---------------------------------------------------------------- 接口请求
function setError(key, message) {
  errors[key] = message || ''
}

// 后端插件尚未初始化时返回 {success: false, message}，需要显式提示而不是显示 0
function apiFailureMessage(body) {
  if (body && body.success === false) {
    return String(body.message || '插件尚未初始化，请保存配置或重新加载插件')
  }
  return ''
}

function showNotice(text, type = 'success') {
  notice.value = { type, text }
}

async function fetchStatus(silent = true) {
  try {
    const body = asObject(unwrapResponse(await props.api.get(`${basePath.value}/status`)))
    const failure = apiFailureMessage(body)
    if (failure) {
      setError('status', failure)
      return false
    }
    status.value = body
    statusLoaded.value = true
    setError('status', '')
    return true
  } catch (error) {
    if (!silent || !statusLoaded.value) setError('status', errorMessage(error, '状态加载失败'))
    return false
  }
}

async function fetchScan(silent = true) {
  try {
    const body = asObject(unwrapResponse(await props.api.get(`${basePath.value}/scan/result`)))
    const failure = apiFailureMessage(body)
    if (failure) {
      setError('scan', failure)
      return false
    }
    scanState.value = body
    setError('scan', '')
    return true
  } catch (error) {
    if (!silent || !scanState.value.result) setError('scan', errorMessage(error, '扫描结果加载失败'))
    return false
  }
}

async function fetchPending(silent = true) {
  try {
    const body = asObject(unwrapResponse(await props.api.get(`${basePath.value}/pending`)))
    const failure = apiFailureMessage(body)
    if (failure) {
      setError('pending', failure)
      return false
    }
    pendingData.value = {
      pending: asArray(body.pending),
      history: asArray(body.history),
    }
    setError('pending', '')
    return true
  } catch (error) {
    if (!silent || !pendingData.value.pending.length) setError('pending', errorMessage(error, '待执行动作加载失败'))
    return false
  }
}

async function refreshAll(silent = true) {
  await Promise.all([fetchStatus(silent), fetchScan(silent), fetchPending(silent)])
}

function scheduleRefresh() {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
  if (disposed || !autoRefresh.value) return
  // 扫描运行中 2 秒轮询，空闲时 10 秒轮询
  const delay = scanRunning.value ? 2000 : 10000
  refreshTimer = window.setTimeout(() => { refreshCycle(true) }, delay)
}

async function refreshCycle(silent = true) {
  if (disposed) return
  const wasScanning = scanRunning.value
  await refreshAll(silent)
  if (wasScanning && !scanRunning.value && !scanError.value) {
    showNotice('扫描已完成，结果已刷新')
  }
  scheduleRefresh()
}

async function manualRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await refreshCycle(false)
  } finally {
    refreshing.value = false
  }
}

async function startScan() {
  if (scanning.value || scanRunning.value) return
  scanning.value = true
  setError('action', '')
  try {
    const body = asObject(unwrapResponse(await props.api.post(`${basePath.value}/scan`, {})))
    const started = body.started !== false && body.success !== false
    showNotice(String(body.message || (started ? '扫描已启动' : '扫描未能启动')), started ? 'info' : 'warning')
    await refreshCycle(true)
  } catch (error) {
    setError('action', errorMessage(error, '扫描启动失败'))
  } finally {
    scanning.value = false
  }
}

// ---------------------------------------------------------------- 删除操作
const confirmState = reactive({
  open: false,
  kind: 'media',
  ids: [],
  total: 0,
  size: 0,
  preview: [],
  warnings: [],
  deleteSidecars: true,
  cleanupEmptyDirs: true,
  deleteFiles: false,
})
const applyResult = ref(null)

function openApplyDialog(kind) {
  const items = kind === 'media' ? selectedMediaItems.value : selectedTorrentItems.value
  if (!items.length) return
  const warnings = []
  const defaults = {
    deleteSidecars: props.initialConfig?.delete_media_sidecars !== false,
    cleanupEmptyDirs: props.initialConfig?.cleanup_empty_dirs !== false,
    deleteFiles: props.initialConfig?.delete_torrent_files !== false,
  }
  let deleteFiles = defaults.deleteFiles
  if (kind === 'torrents') {
    const unsafe = items.filter(item => (
      item?.safe_to_delete_files === false
      || asNumber(item?.shared_torrents) > 0
      || item?.path_exists === false
    ))
    if (unsafe.length) {
      deleteFiles = false
      warnings.push(`${unsafe.length} 个种子与其它任务共享文件、路径不可见或路径不存在，默认保留下载文件`)
    }
    const downloading = items.filter(item => item?.reason === 'downloading')
    if (downloading.length) warnings.push(`${downloading.length} 个种子仍在下载中`)
  } else {
    const noInode = items.filter(item => !asNumber(item?.inode))
    if (noInode.length) warnings.push(`${noInode.length} 个媒体条目缺少 inode，后端会跳过无法校验的条目`)
  }
  applyResult.value = null
  Object.assign(confirmState, {
    open: true,
    kind,
    ids: items.map(item => item?.id).filter(Boolean),
    total: items.length,
    size: items.reduce((total, item) => total + asNumber(item?.size), 0),
    preview: items.slice(0, 8).map(candidateName),
    warnings,
    deleteSidecars: defaults.deleteSidecars,
    cleanupEmptyDirs: defaults.cleanupEmptyDirs,
    deleteFiles,
  })
}

function closeApplyDialog() {
  confirmState.open = false
  applyResult.value = null
}

async function submitApply() {
  if (applying.value || !confirmState.ids.length) return
  applying.value = true
  setError('action', '')
  try {
    const isMedia = confirmState.kind === 'media'
    const body = isMedia
      ? {
        kind: 'media',
        ids: confirmState.ids,
        delete_sidecars: Boolean(confirmState.deleteSidecars),
        cleanup_empty_dirs: Boolean(confirmState.cleanupEmptyDirs),
      }
      : {
        kind: 'torrents',
        ids: confirmState.ids,
        delete_files: Boolean(confirmState.deleteFiles),
      }
    const response = asObject(unwrapResponse(await props.api.post(`${basePath.value}/apply`, body)))
    applyResult.value = {
      success: response.success !== false,
      deleted: asNumber(response.deleted),
      failed: asArray(response.failed),
      warnings: asArray(response.warnings).filter(Boolean),
      message: String(response.message || ''),
    }
    if (applyResult.value.success) {
      if (isMedia) selectedMedia.value = new Set()
      else selectedTorrents.value = new Set()
      showNotice(applyResult.value.message || `已删除 ${applyResult.value.deleted} 项`)
      await refreshCycle(true)
    }
  } catch (error) {
    applyResult.value = {
      success: false,
      deleted: 0,
      failed: [],
      warnings: [],
      message: errorMessage(error, '删除失败'),
    }
  } finally {
    applying.value = false
  }
}

// ---------------------------------------------------------------- 取消待执行
const cancelAllDialog = ref(false)

async function cancelPending(actionId = '') {
  if (cancelling.value) return
  cancelling.value = actionId || '__all__'
  setError('action', '')
  try {
    const body = actionId ? { action_id: actionId } : {}
    const response = asObject(unwrapResponse(await props.api.post(`${basePath.value}/pending/cancel`, body)))
    if (response.success === false) {
      setError('action', String(response.message || '取消待执行动作失败'))
      return
    }
    const count = asNumber(response.cancelled, asNumber(response.count))
    showNotice(String(response.message || (actionId ? '已取消该待执行动作' : `已取消 ${count} 个待执行动作`)))
    cancelAllDialog.value = false
    await refreshCycle(true)
  } catch (error) {
    setError('action', errorMessage(error, '取消待执行动作失败'))
  } finally {
    cancelling.value = ''
  }
}

// ---------------------------------------------------------------- 生命周期
watch(autoRefresh, () => { scheduleRefresh() })

onMounted(async () => {
  clockTimer = window.setInterval(() => {
    nowSeconds.value = Math.floor(Date.now() / 1000)
  }, 1000)
  await refreshCycle(false)
})

onBeforeUnmount(() => {
  disposed = true
  if (refreshTimer) clearTimeout(refreshTimer)
  if (clockTimer) clearInterval(clockTimer)
})
</script>

<template>
  <div class="ms-page">
    <VToolbar density="comfortable" color="transparent" class="ms-toolbar">
      <VIcon class="ms-3" icon="mdi-shield-sync-outline" color="primary" />
      <div class="text-h6 ms-3">媒体同步守护</div>
      <template v-if="statusLoaded">
        <VChip class="ms-3" size="small" :color="enabled ? 'success' : 'grey'" variant="tonal">
          {{ enabled ? '已启用' : '未启用' }}
        </VChip>
        <VChip size="small" :color="dryRun ? 'info' : 'warning'" variant="tonal">
          {{ dryRun ? '演练模式' : '自动执行' }}
        </VChip>
        <VChip v-if="autoMode" size="small" color="warning" variant="tonal">自动开关已开启</VChip>
        <VChip v-if="scanRunning" size="small" color="warning" variant="tonal">
          <VIcon start icon="mdi-sync" class="ms-spin" />
          扫描中
        </VChip>
      </template>
      <VSpacer />
      <VSwitch
        v-model="autoRefresh"
        label="自动刷新"
        density="compact"
        hide-details
        color="primary"
        class="me-2"
      />
      <VBtn variant="text" prepend-icon="mdi-refresh" :loading="refreshing" @click="manualRefresh">刷新</VBtn>
      <VBtn v-if="showSwitch" variant="text" prepend-icon="mdi-cog" @click="emit('switch')">配置</VBtn>
      <VBtn icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
    </VToolbar>
    <VDivider />

    <div class="ms-page-shell">
      <VProgressLinear v-if="!statusLoaded && !errors.status" indeterminate color="primary" class="mb-3" />

      <VAlert
        v-if="errors.action"
        type="error"
        variant="tonal"
        density="compact"
        class="mb-3"
      >
        {{ errors.action }}
        <template #append>
          <VBtn size="small" variant="text" @click="setError('action', '')">关闭</VBtn>
        </template>
      </VAlert>

      <VAlert
        v-if="notice"
        :type="notice.type"
        variant="tonal"
        density="compact"
        class="mb-3"
      >
        {{ notice.text }}
        <template #append>
          <VBtn size="small" variant="text" @click="notice = null">关闭</VBtn>
        </template>
      </VAlert>

      <!-- 状态 / 最近轮询 -->
      <VCard variant="tonal" class="mb-4">
        <VCardText>
          <div class="d-flex flex-wrap align-center ga-2">
            <VIcon icon="mdi-access-point" />
            <span class="text-subtitle-1">最近轮询</span>
            <span class="text-body-2">{{ formatTime(lastPoll.at) }}</span>
            <VChip size="small" variant="tonal" color="grey">耗时 {{ formatDuration(lastPoll.duration) }}</VChip>
            <VChip v-if="!hasPolled" size="small" color="grey" variant="tonal">未轮询</VChip>
            <VChip v-else-if="lastPoll.ok === false" size="small" color="error" variant="tonal">轮询失败</VChip>
            <VChip v-else-if="lastPoll.partial" size="small" color="warning" variant="tonal">部分失败</VChip>
            <VChip v-else-if="lastPoll.ok === true" size="small" color="success" variant="tonal">正常</VChip>
            <VSpacer />
            <span v-if="nextDueAt" class="text-caption text-medium-emphasis">
              最近待办到期：{{ formatRemaining(nextDueAt, nowSeconds) }}
            </span>
          </div>
          <div class="d-flex flex-wrap ga-3 mt-2 text-body-2 text-medium-emphasis">
            <span>媒体 {{ formatCount(lastPoll.media_total) }}</span>
            <span>种子 {{ formatCount(lastPoll.torrent_total) }}</span>
            <span>关联 {{ formatCount(lastPoll.links) }}</span>
            <span>本轮新增候选：媒体 {{ formatCount(lastPoll.lost_media) }} / 种子 {{ formatCount(lastPoll.lost_torrents) }}</span>
            <span>本轮动作：创建 {{ formatCount(lastPoll.actions_created) }} / 执行 {{ formatCount(lastPoll.actions_executed) }}</span>
          </div>

          <VAlert
            v-if="statusWarnings.length"
            type="warning"
            variant="tonal"
            density="compact"
            class="mt-2"
          >
            <div v-for="(warning, index) in statusWarnings" :key="index">• {{ warning }}</div>
          </VAlert>
          <VAlert
            v-if="pollErrors.length"
            type="warning"
            variant="tonal"
            density="compact"
            class="mt-2"
          >
            <div v-for="(item, index) in pollErrors" :key="index">• {{ item }}</div>
          </VAlert>
          <VAlert
            v-if="pollWarnings.length"
            type="info"
            variant="tonal"
            density="compact"
            class="mt-2"
          >
            <div v-for="(item, index) in pollWarnings" :key="index">• {{ item }}</div>
          </VAlert>
          <VAlert
            v-if="safeStatus.last_error"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
          >
            插件错误：{{ safeStatus.last_error }}
          </VAlert>
          <VAlert
            v-if="errors.status"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
          >
            {{ errors.status }}
          </VAlert>
        </VCardText>
      </VCard>

      <!-- 汇总卡片 -->
      <VRow class="mb-2">
        <VCol v-for="card in summaryCards" :key="card.key" cols="12" sm="6" md="3">
          <VCard :color="card.color" variant="tonal">
            <VCardText class="d-flex align-center ga-3">
              <VIcon :icon="card.icon" size="32" />
              <div>
                <div class="text-caption text-medium-emphasis">{{ card.label }}</div>
                <div class="text-h5">{{ formatCount(card.value) }}</div>
              </div>
            </VCardText>
          </VCard>
        </VCol>
      </VRow>

      <!-- 扫描状态 -->
      <VCard class="mb-4">
        <VCardText>
          <div class="d-flex flex-wrap align-center ga-2 mb-2">
            <VIcon icon="mdi-radar" />
            <span class="text-subtitle-1">扫描</span>
            <VChip v-if="scanRunning" size="small" color="warning" variant="tonal">扫描中</VChip>
            <VChip v-else-if="scanResult" size="small" color="success" variant="tonal">已有结果</VChip>
            <VChip v-else size="small" color="grey" variant="tonal">未扫描</VChip>
            <span v-if="scanFinishedAt" class="text-caption text-medium-emphasis">
              完成于 {{ formatTime(scanFinishedAt) }} · 耗时 {{ formatDuration(scanDuration) }}
            </span>
            <VSpacer />
            <VBtn
              color="primary"
              variant="tonal"
              prepend-icon="mdi-play"
              :loading="scanning"
              :disabled="scanRunning"
              @click="startScan"
            >
              开始扫描
            </VBtn>
          </div>

          <VProgressLinear v-if="scanRunning" indeterminate color="warning" class="mb-2" />
          <div v-if="scanRunning" class="text-body-2 text-medium-emphasis mb-2">
            {{ scanProgress || '正在扫描媒体与种子，请稍候…' }}
          </div>
          <VAlert v-if="scanError" type="error" variant="tonal" density="compact" class="mb-2">
            扫描出错：{{ scanError }}
          </VAlert>
          <VAlert v-if="errors.scan" type="error" variant="tonal" density="compact" class="mb-2">
            {{ errors.scan }}
          </VAlert>

          <div v-if="!scanResult && !scanRunning" class="ms-empty text-medium-emphasis">
            尚未扫描。点击“开始扫描”生成媒体 / 种子差异结果（只读，不会删除任何文件）。
          </div>
          <template v-else-if="scanResult">
            <div class="ms-chiprow mb-2">
              <VChip
                v-for="chip in scanStatsChips"
                :key="chip.key"
                size="small"
                variant="tonal"
                color="primary"
              >
                {{ chip.label }} {{ formatCount(chip.value) }}
              </VChip>
            </div>
            <div class="text-caption text-medium-emphasis mb-1">跳过统计</div>
            <div class="ms-chiprow">
              <VChip
                v-for="chip in skippedChips"
                :key="chip.key"
                size="small"
                variant="outlined"
                color="grey"
              >
                {{ chip.label }} {{ formatCount(chip.value) }}
              </VChip>
            </div>
            <div v-if="scanGeneratedAt" class="text-caption text-medium-emphasis mt-2">
              结果生成于 {{ formatTime(scanGeneratedAt) }}
            </div>
          </template>
        </VCardText>
      </VCard>

      <!-- 待执行动作 / 演练候选 -->
      <VCard class="mb-4">
        <VCardText>
          <div class="d-flex flex-wrap align-center ga-2 mb-2">
            <VIcon icon="mdi-timer-sand" />
            <span class="text-subtitle-1">待执行动作</span>
            <VChip size="small" :color="pendingList.length ? 'warning' : 'grey'" variant="tonal">
              {{ pendingList.length }}
            </VChip>
            <VChip v-if="dryRunPendingCount" size="small" color="info" variant="tonal">
              演练 {{ dryRunPendingCount }}
            </VChip>
            <VChip v-if="errors.pending" size="small" color="error" variant="tonal">加载失败</VChip>
            <VSpacer />
            <VBtn
              v-if="candidateCount"
              variant="text"
              size="small"
              prepend-icon="mdi-eye-outline"
              @click="showCandidates = !showCandidates"
            >
              {{ showCandidates ? '收起候选' : `查看候选（${candidateCount}）` }}
            </VBtn>
            <VBtn
              v-if="pendingHistory.length"
              variant="text"
              size="small"
              prepend-icon="mdi-history"
              @click="showHistory = !showHistory"
            >
              {{ showHistory ? '收起历史' : '执行历史' }}
            </VBtn>
            <VBtn
              v-if="pendingList.length"
              color="error"
              variant="tonal"
              size="small"
              prepend-icon="mdi-cancel"
              @click="cancelAllDialog = true"
            >
              全部取消
            </VBtn>
          </div>

          <VAlert v-if="errors.pending" type="error" variant="tonal" density="compact" class="mb-2">
            {{ errors.pending }}
          </VAlert>

          <div v-if="!pendingList.length" class="ms-empty text-medium-emphasis">
            暂无待执行动作。监控候选会在宽限期后按配置自动执行；演练模式下只记录、不执行。
          </div>
          <div v-else class="ms-pending-list">
            <div v-for="action in pendingList" :key="action.action_id" class="ms-pending-item">
              <div class="d-flex flex-wrap align-center ga-2">
                <VChip size="small" :color="action.dry_run ? 'info' : 'warning'" variant="tonal">
                  {{ action.dry_run ? '演练' : '待执行' }}
                </VChip>
                <VChip size="small" variant="outlined">{{ kindLabel(action.kind) }}</VChip>
                <span class="text-body-2">{{ action.reason || '—' }}</span>
                <VSpacer />
                <span v-if="action.due_at" class="text-caption text-medium-emphasis">
                  距执行 {{ formatRemaining(action.due_at, nowSeconds) }}
                </span>
                <VBtn
                  size="small"
                  variant="text"
                  color="error"
                  :loading="cancelling === action.action_id"
                  @click="cancelPending(action.action_id)"
                >
                  取消
                </VBtn>
              </div>
              <div v-if="targetsOf(action).length" class="ms-chiprow mt-1">
                <VChip
                  v-for="target in targetsOf(action)"
                  :key="target"
                  size="x-small"
                  variant="outlined"
                >
                  {{ target }}
                </VChip>
              </div>
            </div>
          </div>

          <template v-if="showCandidates">
            <VDivider class="my-3" />
            <div class="text-subtitle-2 mb-1">最近一轮监控候选（未执行）</div>
            <div v-if="!candidateCount" class="text-body-2 text-medium-emphasis">无候选。</div>
            <div v-else class="ms-candidate-list">
              <div v-for="(item, index) in candidateTorrents" :key="`ct-${index}`" class="ms-candidate-row">
                <VChip size="x-small" color="warning" variant="tonal">种子</VChip>
                <span class="text-body-2 ms-candidate-name">{{ candidateName(item) }}</span>
                <span class="text-caption text-medium-emphasis">{{ item.downloader || '' }}</span>
                <VChip v-if="item.auto_eligible" size="x-small" color="success" variant="tonal">可自动</VChip>
                <VChip v-if="item.blocked_reason" size="x-small" color="grey" variant="tonal">
                  {{ blockedLabel(item.blocked_reason) }}
                </VChip>
              </div>
              <div v-for="(item, index) in candidateMedia" :key="`cm-${index}`" class="ms-candidate-row">
                <VChip size="x-small" color="info" variant="tonal">媒体</VChip>
                <span class="text-body-2 ms-candidate-name">{{ candidateName(item) }}</span>
                <span class="text-caption text-medium-emphasis">{{ item.server || '' }}</span>
                <VChip v-if="item.auto_eligible" size="x-small" color="success" variant="tonal">可自动</VChip>
                <VChip v-if="item.blocked_reason" size="x-small" color="grey" variant="tonal">
                  {{ blockedLabel(item.blocked_reason) }}
                </VChip>
              </div>
            </div>
            <div v-if="dryRun && candidateCount" class="text-caption text-medium-emphasis mt-1">
              当前为演练模式：以上候选不会被执行。
            </div>
          </template>

          <template v-if="showHistory">
            <VDivider class="my-3" />
            <div class="text-subtitle-2 mb-1">执行历史</div>
            <div v-if="!pendingHistory.length" class="text-body-2 text-medium-emphasis">暂无历史。</div>
            <div v-for="(item, index) in pendingHistory" :key="`h-${index}`" class="ms-candidate-row">
              <VChip
                size="x-small"
                :color="item.status === 'done' ? 'success' : (item.status === 'failed' ? 'error' : 'grey')"
                variant="tonal"
              >
                {{ pendingStatusLabel(item.status) }}
              </VChip>
              <span class="text-body-2">{{ kindLabel(item.kind) }}</span>
              <span class="text-body-2 ms-candidate-name">{{ item.summary || item.reason || '—' }}</span>
              <span class="text-caption text-medium-emphasis">{{ formatTime(item.finished_at) }}</span>
            </div>
          </template>
        </VCardText>
      </VCard>

      <!-- 两个页签：媒体无种子 / 种子无媒体 -->
      <VCard>
        <VTabs v-model="activeTab" density="comfortable">
          <VTab value="media">
            媒体无种子
            <VChip class="ms-tab-chip" size="x-small" variant="tonal">{{ formatCount(mediaItems.length) }}</VChip>
          </VTab>
          <VTab value="torrents">
            种子无媒体
            <VChip class="ms-tab-chip" size="x-small" variant="tonal">{{ formatCount(torrentItems.length) }}</VChip>
          </VTab>
        </VTabs>
        <VDivider />

        <VWindow v-model="activeTab">
          <!-- 媒体无种子 -->
          <VWindowItem value="media">
            <div class="ms-tab-body">
              <div class="ms-filterbar">
                <VTextField
                  v-model="mediaSearchText"
                  density="compact"
                  variant="outlined"
                  label="搜索媒体名称 / 路径 / 剧集"
                  prepend-inner-icon="mdi-magnify"
                  hide-details
                  clearable
                  class="ms-search"
                />
                <VSelect
                  v-model="mediaTypeFilter"
                  :items="mediaTypeOptions"
                  density="compact"
                  variant="outlined"
                  label="类型"
                  hide-details
                  class="ms-select"
                />
                <VSpacer />
                <VBtn
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-radar"
                  :loading="scanning"
                  :disabled="scanRunning"
                  @click="startScan"
                >
                  扫描
                </VBtn>
              </div>

              <div v-if="mediaGroupStates.length" class="ms-groups">
                <div class="text-caption text-medium-emphasis mb-1">按分组整组选择</div>
                <div class="ms-group-list">
                  <div
                    v-for="entry in mediaGroupStates"
                    :key="entry.group.group_id || entry.group.title"
                    class="ms-group-item"
                  >
                    <VCheckbox
                      :model-value="entry.selection.checked"
                      :indeterminate="entry.selection.indeterminate"
                      density="compact"
                      hide-details
                      @update:model-value="value => selectGroup('media', entry.ids, value)"
                    />
                    <div class="ms-group-label">
                      <div class="text-body-2">{{ entry.group.title || entry.group.group_id }}</div>
                      <div class="text-caption text-medium-emphasis">
                        {{ entry.selection.count }} / {{ entry.selection.total }} 项 · {{ formatSize(entry.group.size) }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div class="ms-selection-bar">
                <VCheckbox
                  :model-value="allMediaSelected"
                  :indeterminate="someMediaSelected"
                  density="compact"
                  hide-details
                  label="全选当前筛选结果"
                  @update:model-value="selectAllMedia"
                />
                <span class="text-caption text-medium-emphasis">
                  显示 {{ formatCount(filteredMedia.length) }} / 共 {{ formatCount(mediaItems.length) }} 项
                </span>
                <VSpacer />
                <VBtn v-if="selectedMediaCount" variant="text" size="small" @click="clearSelection('media')">
                  清除选择
                </VBtn>
              </div>

              <VAlert v-if="!scanResult && !scanRunning" type="info" variant="tonal" density="compact" class="mb-2">
                尚未扫描，请先点击“扫描”。
              </VAlert>
              <div v-else-if="!filteredMedia.length" class="ms-empty text-medium-emphasis">
                {{ mediaItems.length ? '没有匹配的媒体条目' : '扫描结果中没有“媒体无种子”条目' }}
              </div>
              <div v-else class="ms-table-wrap">
                <VTable density="compact" hover class="ms-table">
                  <thead>
                    <tr>
                      <th class="ms-col-check">
                        <VCheckbox
                          :model-value="allMediaSelected"
                          :indeterminate="someMediaSelected"
                          density="compact"
                          hide-details
                          aria-label="全选媒体"
                          @update:model-value="selectAllMedia"
                        />
                      </th>
                      <th class="text-start">媒体</th>
                      <th class="text-start">类型</th>
                      <th class="text-start">路径</th>
                      <th class="text-end">大小</th>
                      <th class="text-start">历史</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(item, index) in filteredMedia" :key="item.id || item.path || index">
                      <td>
                        <VCheckbox
                          :model-value="selectedMedia.has(item.id)"
                          density="compact"
                          hide-details
                          :aria-label="`选择 ${item.display_title || item.name || item.id}`"
                          @update:model-value="value => toggleMediaSelection(item.id, value)"
                        />
                      </td>
                      <td class="ms-name-cell">
                        <div class="text-body-2">{{ item.display_title || item.name || item.id || '—' }}</div>
                        <div v-if="item.series_name" class="text-caption text-medium-emphasis">
                          {{ item.series_name }}
                          <template v-if="item.season !== null && item.season !== undefined"> · S{{ item.season }}</template>
                          <template v-if="item.episode !== null && item.episode !== undefined"> E{{ item.episode }}</template>
                        </div>
                        <VChip v-if="item.reason" size="x-small" variant="tonal" color="grey">
                          {{ mediaReasonLabel(item.reason) }}
                        </VChip>
                      </td>
                      <td>
                        <VChip
                          size="small"
                          variant="tonal"
                          :color="item.item_type === 'Movie' ? 'primary' : 'info'"
                        >
                          {{ itemTypeLabel(item.item_type) }}
                        </VChip>
                      </td>
                      <td class="ms-path-cell">
                        <div class="text-body-2">{{ item.path || item.folder || '—' }}</div>
                        <div v-if="item.folder && item.path !== item.folder" class="text-caption text-medium-emphasis">
                          {{ item.folder }}
                        </div>
                      </td>
                      <td class="text-end text-body-2">{{ formatSize(item.size) }}</td>
                      <td>
                        <VChip v-if="asArray(item.history_hashes).length" size="small" variant="tonal" color="teal">
                          {{ asArray(item.history_hashes).length }} 条
                        </VChip>
                        <span v-else class="text-caption text-medium-emphasis">—</span>
                      </td>
                    </tr>
                  </tbody>
                </VTable>
              </div>

              <!-- 底部操作栏：删除选中媒体 -->
              <div class="ms-action-bar">
                <span class="text-body-2">
                  已选择 {{ selectedMediaCount }} 项媒体
                  <template v-if="selectedMediaCount">（{{ formatSize(selectedMediaSize) }}）</template>
                </span>
                <VSpacer />
                <VBtn
                  v-if="selectedMediaCount"
                  variant="text"
                  size="small"
                  @click="clearSelection('media')"
                >
                  清除选择
                </VBtn>
                <VBtn
                  color="error"
                  variant="flat"
                  size="small"
                  prepend-icon="mdi-delete"
                  :disabled="!selectedMediaCount"
                  @click="openApplyDialog('media')"
                >
                  删除选中媒体（{{ selectedMediaCount }}）
                </VBtn>
              </div>
            </div>
          </VWindowItem>

          <!-- 种子无媒体 -->
          <VWindowItem value="torrents">
            <div class="ms-tab-body">
              <div class="ms-filterbar">
                <VTextField
                  v-model="torrentSearchText"
                  density="compact"
                  variant="outlined"
                  label="搜索种子名称 / hash / 下载器 / 路径"
                  prepend-inner-icon="mdi-magnify"
                  hide-details
                  clearable
                  class="ms-search"
                />
                <VSelect
                  v-model="torrentCategoryFilter"
                  :items="torrentCategoryOptions"
                  density="compact"
                  variant="outlined"
                  label="分类"
                  hide-details
                  class="ms-select"
                />
                <VSpacer />
                <VBtn
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-radar"
                  :loading="scanning"
                  :disabled="scanRunning"
                  @click="startScan"
                >
                  扫描
                </VBtn>
              </div>

              <div v-if="torrentGroupStates.length" class="ms-groups">
                <div class="text-caption text-medium-emphasis mb-1">按分组整组选择</div>
                <div class="ms-group-list">
                  <div
                    v-for="entry in torrentGroupStates"
                    :key="entry.group.group_id || entry.group.title"
                    class="ms-group-item"
                  >
                    <VCheckbox
                      :model-value="entry.selection.checked"
                      :indeterminate="entry.selection.indeterminate"
                      density="compact"
                      hide-details
                      @update:model-value="value => selectGroup('torrents', entry.ids, value)"
                    />
                    <div class="ms-group-label">
                      <div class="text-body-2">{{ entry.group.title || entry.group.group_id }}</div>
                      <div class="text-caption text-medium-emphasis">
                        {{ entry.selection.count }} / {{ entry.selection.total }} 项 · {{ formatSize(entry.group.size) }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div class="ms-selection-bar">
                <VCheckbox
                  :model-value="allTorrentsSelected"
                  :indeterminate="someTorrentsSelected"
                  density="compact"
                  hide-details
                  label="全选当前筛选结果"
                  @update:model-value="selectAllTorrents"
                />
                <span class="text-caption text-medium-emphasis">
                  显示 {{ formatCount(filteredTorrents.length) }} / 共 {{ formatCount(torrentItems.length) }} 项
                </span>
                <VSpacer />
                <VBtn v-if="selectedTorrentCount" variant="text" size="small" @click="clearSelection('torrents')">
                  清除选择
                </VBtn>
              </div>

              <VAlert v-if="!scanResult && !scanRunning" type="info" variant="tonal" density="compact" class="mb-2">
                尚未扫描，请先点击“扫描”。
              </VAlert>
              <div v-else-if="!filteredTorrents.length" class="ms-empty text-medium-emphasis">
                {{ torrentItems.length ? '没有匹配的种子' : '扫描结果中没有“种子无媒体”条目' }}
              </div>
              <div v-else class="ms-table-wrap">
                <VTable density="compact" hover class="ms-table">
                  <thead>
                    <tr>
                      <th class="ms-col-check">
                        <VCheckbox
                          :model-value="allTorrentsSelected"
                          :indeterminate="someTorrentsSelected"
                          density="compact"
                          hide-details
                          aria-label="全选种子"
                          @update:model-value="selectAllTorrents"
                        />
                      </th>
                      <th class="text-start">种子</th>
                      <th class="text-start">下载器</th>
                      <th class="text-start">分类</th>
                      <th class="text-start">状态</th>
                      <th class="text-end">大小</th>
                      <th class="text-start">路径</th>
                      <th class="text-start">删除安全</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(item, index) in filteredTorrents"
                      :key="item.id || item.hash || index"
                      :class="{ 'ms-row-ignored': Boolean(item.ignored_reason) }"
                    >
                      <td>
                        <VCheckbox
                          :model-value="selectedTorrents.has(item.id)"
                          :disabled="Boolean(item.ignored_reason)"
                          density="compact"
                          hide-details
                          :aria-label="`选择 ${item.name || item.id}`"
                          @update:model-value="value => toggleTorrentSelection(item.id, value)"
                        />
                      </td>
                      <td class="ms-name-cell">
                        <div class="text-body-2">{{ item.name || item.hash || item.id || '—' }}</div>
                        <div class="text-caption text-medium-emphasis">{{ item.hash || '' }}</div>
                        <div class="ms-chiprow mt-1">
                          <VChip
                            v-for="tag in asArray(item.tags)"
                            :key="tag"
                            size="x-small"
                            variant="outlined"
                            color="grey"
                          >
                            {{ tag }}
                          </VChip>
                          <VChip v-if="item.ignored_reason" size="x-small" color="grey" variant="tonal">
                            {{ ignoredLabel(item.ignored_reason) }}
                          </VChip>
                          <VChip v-if="item.reason" size="x-small" color="grey" variant="tonal">
                            {{ torrentReasonLabel(item.reason) }}
                          </VChip>
                        </div>
                      </td>
                      <td class="text-body-2">{{ item.downloader || '—' }}</td>
                      <td class="text-body-2">{{ item.category || '未分类' }}</td>
                      <td>
                        <VChip v-if="item.completed" size="small" color="success" variant="tonal">已完成</VChip>
                        <VChip v-else size="small" color="warning" variant="tonal">{{ progressPercent(item.progress) }}%</VChip>
                        <div class="text-caption text-medium-emphasis">{{ item.state || '' }}</div>
                      </td>
                      <td class="text-end text-body-2">{{ formatSize(item.size) }}</td>
                      <td class="ms-path-cell">
                        <div class="text-body-2">{{ item.content_path || item.save_path || '—' }}</div>
                        <VChip v-if="item.path_exists === false" size="x-small" color="warning" variant="tonal">
                          路径不可见
                        </VChip>
                      </td>
                      <td>
                        <template v-if="item.ignored_reason">
                          <span class="text-caption text-medium-emphasis">—</span>
                        </template>
                        <template v-else>
                          <VChip
                            size="small"
                            :color="item.safe_to_delete_files ? 'success' : 'warning'"
                            variant="tonal"
                          >
                            {{ item.safe_to_delete_files ? '可删文件' : '保留文件' }}
                          </VChip>
                          <div v-if="asNumber(item.shared_torrents) > 0" class="text-caption text-medium-emphasis">
                            共享 {{ asNumber(item.shared_torrents) }}
                          </div>
                          <div v-else class="text-caption text-medium-emphasis">
                            {{ asNumber(item.video_files) }} 个视频
                          </div>
                        </template>
                      </td>
                    </tr>
                  </tbody>
                </VTable>
              </div>

              <!-- 底部操作栏：删除选中种子 -->
              <div class="ms-action-bar">
                <span class="text-body-2">
                  已选择 {{ selectedTorrentCount }} 个种子
                  <template v-if="selectedTorrentCount">（{{ formatSize(selectedTorrentSize) }}）</template>
                </span>
                <VSpacer />
                <VBtn
                  v-if="selectedTorrentCount"
                  variant="text"
                  size="small"
                  @click="clearSelection('torrents')"
                >
                  清除选择
                </VBtn>
                <VBtn
                  color="error"
                  variant="flat"
                  size="small"
                  prepend-icon="mdi-delete"
                  :disabled="!selectedTorrentCount"
                  @click="openApplyDialog('torrents')"
                >
                  删除选中种子（{{ selectedTorrentCount }}）
                </VBtn>
              </div>
            </div>
          </VWindowItem>
        </VWindow>
      </VCard>
    </div>

    <!-- 删除二次确认 -->
    <VDialog v-model="confirmState.open" max-width="680" persistent>
      <VCard>
        <VCardTitle class="d-flex align-center">
          <VIcon :icon="confirmState.kind === 'media' ? 'mdi-filmstrip' : 'mdi-download'" class="me-2" />
          {{ confirmState.kind === 'media' ? '删除选中媒体' : '删除选中种子' }}
        </VCardTitle>
        <VCardText>
          <template v-if="!applyResult">
            <div class="text-body-1 mb-2">
              即将删除 <strong>{{ confirmState.total }}</strong>
              项{{ confirmState.kind === 'media' ? '媒体条目' : '种子' }}（合计 {{ formatSize(confirmState.size) }}），操作不可撤销。
            </div>
            <VAlert v-if="dryRun" type="info" variant="tonal" density="compact" class="mb-2">
              当前为演练模式：监控产生的自动动作不会执行，但本次手动确认的删除会立即执行。
            </VAlert>
            <VAlert
              v-for="(warning, index) in confirmState.warnings"
              :key="index"
              type="warning"
              variant="tonal"
              density="compact"
              class="mb-2"
            >
              {{ warning }}
            </VAlert>
            <div class="ms-preview">
              <div v-for="(name, index) in confirmState.preview" :key="index" class="text-body-2 ms-preview-item">
                • {{ name }}
              </div>
              <div v-if="confirmState.total > confirmState.preview.length" class="text-caption text-medium-emphasis">
                …等共 {{ confirmState.total }} 项
              </div>
            </div>
            <VDivider class="my-2" />
            <template v-if="confirmState.kind === 'media'">
              <VCheckbox
                v-model="confirmState.deleteSidecars"
                density="compact"
                hide-details
                label="删除附属文件（字幕 / nfo / 图片）"
              />
              <VCheckbox
                v-model="confirmState.cleanupEmptyDirs"
                density="compact"
                hide-details
                label="清理删除后产生的空目录"
              />
            </template>
            <template v-else>
              <VCheckbox
                v-model="confirmState.deleteFiles"
                density="compact"
                hide-details
                label="同时删除下载文件（不勾选则仅删除种子任务）"
              />
              <div class="text-caption text-medium-emphasis mt-1">
                共享文件保护：与其他现存种子共享文件的种子，后端会强制保留下载文件。
              </div>
            </template>
          </template>

          <template v-else>
            <VAlert
              :type="applyResult.success ? 'success' : 'error'"
              variant="tonal"
              density="compact"
              class="mb-2"
            >
              {{ applyResult.message || (applyResult.success ? `已删除 ${applyResult.deleted} 项` : '删除失败') }}
            </VAlert>
            <div v-if="applyResult.deleted" class="text-body-2 mb-2">删除数量：{{ applyResult.deleted }}</div>
            <div v-if="applyResult.failed.length" class="mb-2">
              <div class="text-subtitle-2 mb-1">失败条目</div>
              <div v-for="(failure, index) in applyResult.failed" :key="index" class="text-body-2">
                {{ failure.id || '—' }}：{{ failure.error || '未知错误' }}
              </div>
            </div>
            <div v-if="applyResult.warnings.length">
              <div class="text-subtitle-2 mb-1">警告</div>
              <div v-for="(warning, index) in applyResult.warnings" :key="index" class="text-body-2">
                • {{ warning }}
              </div>
            </div>
          </template>
        </VCardText>
        <VCardActions>
          <VSpacer />
          <template v-if="!applyResult">
            <VBtn variant="text" @click="closeApplyDialog">取消</VBtn>
            <VBtn color="error" prepend-icon="mdi-delete" :loading="applying" @click="submitApply">
              确认删除
            </VBtn>
          </template>
          <VBtn v-else color="primary" variant="flat" @click="closeApplyDialog">关闭</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

    <!-- 全部取消待执行动作确认 -->
    <VDialog v-model="cancelAllDialog" max-width="460">
      <VCard>
        <VCardTitle>取消全部待执行动作？</VCardTitle>
        <VCardText>
          将取消当前 {{ pendingList.length }} 个待执行动作；已完成的动作不受影响。
        </VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="cancelAllDialog = false">返回</VBtn>
          <VBtn color="error" :loading="cancelling === '__all__'" @click="cancelPending('')">全部取消</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>
  </div>
</template>

<style scoped>
.ms-page {
  background: rgb(var(--v-theme-background));
  min-height: 100%;
}

.ms-toolbar {
  background: transparent;
}

.ms-toolbar :deep(.v-toolbar__content) {
  flex-wrap: wrap;
  gap: 6px;
  height: auto !important;
  padding-bottom: 6px;
  padding-top: 6px;
}

.ms-page-shell {
  padding: 18px;
}

.ms-empty {
  padding: 28px 16px;
  text-align: center;
}

.ms-spin {
  animation: ms-spin 1.4s linear infinite;
}

@keyframes ms-spin {
  to {
    transform: rotate(360deg);
  }
}

.ms-chiprow {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ms-filterbar {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-bottom: 12px;
}

.ms-search {
  flex: 1 1 280px;
  max-width: 460px;
}

.ms-select {
  flex: 0 1 180px;
  min-width: 140px;
}

.ms-groups {
  background: rgba(var(--v-theme-on-surface), 0.03);
  border-radius: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
}

.ms-group-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  max-height: 168px;
  overflow-y: auto;
}

.ms-group-item {
  align-items: center;
  display: flex;
  gap: 6px;
  max-width: 360px;
  min-width: 200px;
}

.ms-group-label {
  min-width: 0;
}

.ms-selection-bar {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.ms-action-bar {
  align-items: center;
  background: rgba(var(--v-theme-on-surface), 0.03);
  border-radius: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding: 8px 10px;
}

.ms-table-wrap {
  overflow-x: auto;
  width: 100%;
}

.ms-table {
  min-width: 820px;
}

.ms-col-check {
  width: 48px;
}

.ms-name-cell {
  max-width: 320px;
  min-width: 180px;
}

.ms-path-cell {
  max-width: 360px;
  min-width: 180px;
  overflow-wrap: anywhere;
}

.ms-row-ignored {
  opacity: 0.55;
}

.ms-pending-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ms-pending-item {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.09);
  border-radius: 8px;
  padding: 8px 10px;
}

.ms-candidate-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ms-candidate-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ms-candidate-name {
  max-width: 520px;
  overflow-wrap: anywhere;
}

.ms-tab-chip {
  margin-inline-start: 6px;
}

.ms-tab-body {
  padding: 14px 16px 18px;
}

.ms-preview {
  border-left: 3px solid rgba(var(--v-theme-primary), 0.4);
  margin: 8px 0;
  padding-left: 10px;
}

.ms-preview-item {
  overflow-wrap: anywhere;
}

@media (max-width: 600px) {
  .ms-page-shell {
    padding: 10px;
  }

  .ms-tab-body {
    padding: 10px;
  }

  .ms-search {
    flex-basis: 100%;
    max-width: none;
  }

  .ms-select {
    flex-basis: 100%;
  }
}
</style>
