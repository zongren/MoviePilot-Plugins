import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import Config from '../../src/components/Config.vue'
import Page from '../../src/components/Page.vue'
import {
  errorMessage,
  formatCount,
  formatDuration,
  formatRemaining,
  formatSize,
  unwrapResponse,
} from '../../src/provider.js'
import applyFixture from './fixtures/apply.json'
import pendingFixture from './fixtures/pending.json'
import scanFixture from './fixtures/scan_result.json'
import statusFixture from './fixtures/status.json'
import formFixture from './fixtures/plugin-form.json'

// 与宿主一致：Vuetify 组件全局注册，插件前端组件只依赖 resolveComponent。
const vuetify = createVuetify({ components, directives })

let wrapper = null

afterEach(() => {
  if (wrapper) {
    wrapper.unmount()
    wrapper = null
  }
  document.body.innerHTML = ''
})

function mountComponent(component, props) {
  wrapper = mount(component, {
    props,
    global: { plugins: [vuetify] },
    attachTo: document.body,
  })
  return wrapper
}

function findButton(text, root = document) {
  return Array.from(root.querySelectorAll('button'))
    .find(node => (node.textContent || '').includes(text))
}

function findRow(text, root = document) {
  return Array.from(root.querySelectorAll('tr'))
    .find(node => (node.textContent || '').includes(text))
}

function clickTab(text) {
  const tab = Array.from(document.querySelectorAll('.v-tab'))
    .find(node => (node.textContent || '').includes(text))
  expect(tab, `页签 ${text} 应存在`).toBeTruthy()
  tab.click()
}

// 模拟宿主 axios：get/post 返回裸对象，unwrapResponse 负责兼容包装结构
function createPageApi(overrides = {}) {
  const calls = { get: [], post: [] }
  const api = {
    get: vi.fn(async url => {
      calls.get.push(url)
      if (url === 'plugin/MediaSyncGuard/status') return overrides.status ?? statusFixture
      if (url === 'plugin/MediaSyncGuard/scan/result') return overrides.scan ?? scanFixture
      if (url === 'plugin/MediaSyncGuard/pending') return overrides.pending ?? pendingFixture
      throw new Error(`unexpected GET ${url}`)
    }),
    post: vi.fn(async (url, body) => {
      calls.post.push({ url, body })
      if (url === 'plugin/MediaSyncGuard/scan') return { started: true, message: '扫描已启动' }
      if (url === 'plugin/MediaSyncGuard/apply') return overrides.apply ?? applyFixture
      if (url === 'plugin/MediaSyncGuard/pending/cancel') return { success: true, cancelled: 1 }
      throw new Error(`unexpected POST ${url}`)
    }),
  }
  return { api, calls }
}

function emptyScanPayload() {
  return {
    running: false,
    started_at: 0,
    finished_at: 0,
    duration: 0,
    progress: '',
    error: '',
    result: null,
  }
}

function emptyStatusPayload() {
  return {
    enabled: false,
    dry_run: true,
    configured: false,
    warnings: [],
    selected_downloaders: [],
    selected_media_servers: [],
    auto: { media_to_torrent: false, torrent_to_media: false },
    last_poll: null,
    pending: { count: 0, dry_run_count: 0, next_due_at: 0 },
    candidates: null,
    scan: { running: false, has_result: false, last_generated_at: 0 },
    last_error: '',
  }
}

describe('MediaSyncGuard Config.vue', () => {
  it('renders the backend JSON form and emits the full config model on save', async () => {
    const api = {
      get: vi.fn(async url => {
        expect(url).toBe('plugin/form/MediaSyncGuard')
        // 用 data.data 包装验证 unwrapResponse 兼容宿主 axios
        return { data: { data: formFixture } }
      }),
    }
    const view = mountComponent(Config, {
      api,
      initialConfig: { notify: true },
      pluginId: 'MediaSyncGuard',
    })
    await flushPromises()

    expect(view.text()).toContain('启用插件')
    expect(view.text()).toContain('演练模式（仅记录/通知，不自动删除）')
    expect(view.text()).toContain('监控间隔（秒）')
    expect(view.text()).toContain('未检测到已启用的 Jellyfin 媒体服务器')

    findButton('保存').click()
    await flushPromises()

    const saved = view.emitted('save')
    expect(saved).toBeTruthy()
    expect(saved[0][0]).toMatchObject({
      enabled: true,
      dry_run: true,
      downloaders: ['主QB'],
      poll_seconds: 60,
      notify: true,
    })

    findButton('管理页').click()
    expect(view.emitted('switch')).toBeTruthy()
  })

  it('shows a retryable empty state when the backend form is empty', async () => {
    const api = { get: vi.fn(async () => ({ conf: [], model: {} })) }
    const view = mountComponent(Config, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    expect(view.text()).toContain('暂无可配置表单')
    expect(findButton('重试')).toBeTruthy()
  })
})

describe('MediaSyncGuard Page.vue', () => {
  it('renders status chips, summary cards, scan stats and the media table', async () => {
    const { api } = createPageApi()
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith('plugin/MediaSyncGuard/status')
    expect(api.get).toHaveBeenCalledWith('plugin/MediaSyncGuard/scan/result')
    expect(api.get).toHaveBeenCalledWith('plugin/MediaSyncGuard/pending')

    const text = view.text()
    expect(text).toContain('媒体同步守护')
    expect(text).toContain('已启用')
    expect(text).toContain('演练模式')
    expect(text).toContain('最近轮询')
    expect(text).toContain('耗时 3.2 秒')
    expect(text).toContain('媒体条目')
    expect(text).toContain('硬链接关联')
    expect(text).toContain('8,268')
    expect(text).toContain('3,778')
    expect(text).toContain('8,140')
    expect(text).toContain('老友记')
    expect(text).toContain('2026-') // formatTime 渲染最近轮询时间
    expect(text).toContain('已有结果')
    expect(text).toContain('跳过统计')
  })

  it('switches to the torrents tab and shows torrent rows with ignore/safety state', async () => {
    const { api } = createPageApi()
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    clickTab('种子无媒体')
    await flushPromises()

    expect(view.text()).toContain('Stuart.Fails.To.Save.The.Universe.S01.Complete.1080p')
    expect(view.text()).toContain('主QB')
    expect(view.text()).toContain('可删文件')
    expect(view.text()).toContain('MoviePilot自动辅种')
    expect(view.text()).toContain('删除选中种子（0）')
  })

  it('confirms torrent deletion via dialog and posts the apply payload', async () => {
    const { api } = createPageApi()
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    clickTab('种子无媒体')
    await flushPromises()

    const row = findRow('Stuart.Fails')
    expect(row).toBeTruthy()
    row.querySelector('input[type="checkbox"]').click()
    await flushPromises()

    findButton('删除选中种子（1）').click()
    await flushPromises()

    expect(document.body.textContent).toContain('删除选中种子')
    expect(document.body.textContent).toContain('操作不可撤销')
    findButton('确认删除').click()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('plugin/MediaSyncGuard/apply', {
      kind: 'torrents',
      ids: ['主QB|deadbeef'],
      delete_files: true,
    })
    expect(document.body.textContent).toContain('已删除 1 个媒体条目')
    expect(findButton('关闭')).toBeTruthy()
    // 删除成功后刷新扫描结果
    expect(api.get.mock.calls.filter(([url]) => url === 'plugin/MediaSyncGuard/scan/result').length)
      .toBeGreaterThanOrEqual(2)

    view.unmount()
    wrapper = null
  })

  it('confirms media deletion with sidecar and empty-dir options', async () => {
    const { api } = createPageApi()
    mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    const row = findRow('老友记')
    expect(row).toBeTruthy()
    row.querySelector('input[type="checkbox"]').click()
    await flushPromises()

    findButton('删除选中媒体（1）').click()
    await flushPromises()
    findButton('确认删除').click()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('plugin/MediaSyncGuard/apply', {
      kind: 'media',
      ids: ['Jellyfin|abc'],
      delete_sidecars: true,
      cleanup_empty_dirs: true,
    })
    expect(document.body.textContent).toContain('已删除 1 个媒体条目')
  })

  it('filters by category without changing selection', async () => {
    const { api } = createPageApi()
    mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    const categoryButtons = Array.from(document.querySelectorAll('.ms-category-item'))
    expect(categoryButtons.length).toBeGreaterThan(1)
    const activeCategory = categoryButtons.find(node => (node.textContent || '').includes('老友记'))
    expect(activeCategory, '媒体分类列表中应包含分组分类').toBeTruthy()

    // 点击分类只做筛选，不勾选任何条目
    activeCategory.click()
    await flushPromises()
    expect(activeCategory.classList.contains('ms-category-item--active')).toBe(true)
    expect(findButton('删除选中媒体（0）'), '分类筛选不应改变已选择数').toBeTruthy()

    // 再点“全部”恢复
    const allCategory = categoryButtons.find(node => (node.textContent || '').trim().startsWith('全部'))
    expect(allCategory).toBeTruthy()
    allCategory.click()
    await flushPromises()
    expect(allCategory.classList.contains('ms-category-item--active')).toBe(true)
  })

  it('renders downloader temp-file rows and only allows deleting orphans', async () => {
    const scan = JSON.parse(JSON.stringify(scanFixture))
    scan.result.temp_files = [
      {
        id: 'temp:owned',
        path: '/medias/Downloads/TV/pack/ep.mkv.!qB',
        name: 'ep.mkv.!qB',
        suffix: '.!qb',
        size: 100,
        deletable: false,
        reason: 'owned_by_torrent',
        owner_hashes: ['hash1'],
        owner_names: ['Pack Task'],
      },
      {
        id: 'temp:orphan',
        path: '/medias/Downloads/TV/pack/junk.mkv.!qB',
        name: 'junk.mkv.!qB',
        suffix: '.!qb',
        size: 50,
        deletable: true,
        reason: 'orphan_temp_file',
        owner_hashes: [],
        owner_names: [],
      },
    ]
    scan.result.temp_file_stats = {
      temp_files: 2,
      owned_temp_files: 1,
      orphan_temp_files: 1,
      check_failed_temp_files: 0,
    }
    const { api } = createPageApi({ scan })
    mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    clickTab('下载器临时文件')
    await flushPromises()

    const text = document.body.textContent || ''
    expect(text).toContain('ep.mkv.!qB')
    expect(text).toContain('junk.mkv.!qB')
    expect(text).toContain('属于现存任务')
    expect(text).toContain('可清理')
    // 属于现存任务的行复选框必须禁用，无主残留可选
    const rows = Array.from(document.querySelectorAll('.ms-table tbody tr'))
    const ownedRow = rows.find(row => (row.textContent || '').includes('ep.mkv.!qB'))
    const orphanRow = rows.find(row => (row.textContent || '').includes('junk.mkv.!qB'))
    expect(ownedRow.querySelector('.v-checkbox input').disabled).toBe(true)
    expect(orphanRow.querySelector('.v-checkbox input').disabled).toBe(false)
  })

  it('cancels a single pending action through the pending/cancel API', async () => {
    const { api } = createPageApi()
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    expect(view.text()).toContain('待执行动作')
    expect(view.text()).toContain('媒体条目已从 Jellyfin 删除')
    expect(view.text()).toContain('主QB|deadbeef')

    const cancelButton = Array.from(view.element.querySelectorAll('button'))
      .find(node => (node.textContent || '').trim() === '取消')
    expect(cancelButton).toBeTruthy()
    cancelButton.click()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('plugin/MediaSyncGuard/pending/cancel', {
      action_id: 'act-1',
    })
    expect(view.text()).toContain('已取消该待执行动作')
  })

  it('starts a scan and refreshes the scan result', async () => {
    const { api } = createPageApi()
    mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    findButton('开始扫描').click()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('plugin/MediaSyncGuard/scan', {})
    expect(api.get.mock.calls.filter(([url]) => url === 'plugin/MediaSyncGuard/scan/result').length)
      .toBeGreaterThanOrEqual(2)
  })

  it('renders empty and null API payloads without crashing', async () => {
    const { api } = createPageApi({
      status: emptyStatusPayload(),
      scan: emptyScanPayload(),
      pending: { pending: [], history: [] },
    })
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    const text = view.text()
    expect(text).toContain('未启用')
    expect(text).toContain('尚未扫描')
    expect(text).toContain('暂无待执行动作')

    clickTab('种子无媒体')
    await flushPromises()
    expect(view.text()).toContain('尚未扫描，请先点击“扫描”。')
  })

  it('surfaces the backend not-initialized message instead of showing zeros', async () => {
    const failure = { success: false, message: '插件尚未初始化，请保存配置或重新加载插件' }
    const { api } = createPageApi({ status: failure, scan: failure, pending: failure })
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    expect(view.text()).toContain('插件尚未初始化，请保存配置或重新加载插件')
  })

  it('shows ignored torrents as disabled and not selectable', async () => {
    const ignoredScan = JSON.parse(JSON.stringify(scanFixture))
    ignoredScan.result.torrents_without_media[0].ignored_reason = 'ignored_tag'
    ignoredScan.result.torrents_without_media[0].safe_to_delete_files = false
    const { api } = createPageApi({ scan: ignoredScan })
    const view = mountComponent(Page, { api, pluginId: 'MediaSyncGuard' })
    await flushPromises()

    clickTab('种子无媒体')
    await flushPromises()

    expect(view.text()).toContain('忽略标签')
    const row = findRow('Stuart.Fails')
    const checkbox = row.querySelector('input[type="checkbox"]')
    expect(checkbox.disabled).toBe(true)
  })
})

describe('provider helpers', () => {
  it('unwraps wrapped and unwrapped axios responses', () => {
    expect(unwrapResponse({ data: { data: { ok: 1 } } })).toEqual({ ok: 1 })
    expect(unwrapResponse({ data: { ok: 1 } })).toEqual({ ok: 1 })
    expect(unwrapResponse({ ok: 1 })).toEqual({ ok: 1 })
    expect(unwrapResponse(null)).toBeNull()
  })

  it('extracts API error messages', () => {
    expect(errorMessage({ response: { data: { detail: 'boom' } } })).toBe('boom')
    expect(errorMessage({ response: { data: { message: 'bad' } } })).toBe('bad')
    expect(errorMessage({ message: 'network' })).toBe('network')
    expect(errorMessage(null, '兜底')).toBe('兜底')
  })

  it('formats sizes, durations, remaining time and counts', () => {
    expect(formatSize(0)).toBe('-')
    expect(formatSize(1536)).toBe('1.5 KB')
    expect(formatSize(9999999)).toBe('9.5 MB')
    expect(formatDuration(0)).toBe('-')
    expect(formatDuration(3.2)).toBe('3.2 秒')
    expect(formatDuration(65)).toBe('1 分 5 秒')
    expect(formatRemaining(0, 100)).toBe('-')
    expect(formatRemaining(130, 100)).toBe('30 秒')
    expect(formatRemaining(220, 100)).toBe('2 分')
    expect(formatRemaining(50, 100)).toBe('已到期')
    expect(formatCount(8268)).toBe('8,268')
    expect(formatCount(null)).toBe('0')
  })
})
