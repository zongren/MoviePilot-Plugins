import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import Config from '../../src/components/Config.vue'
import Page from '../../src/components/Page.vue'
import { baseName, formatSize, isVideoFile, joinPath, parentPath, splitPathLines } from '../../src/provider.js'
import formFixture from './fixtures/plugin-form.json'
import storageFixture from './fixtures/storage-list.json'

// 与宿主一致：Vuetify 组件全局注册，插件前端组件只依赖 resolveComponent。
const vuetify = createVuetify({ components, directives })

function mountComponent(component, props) {
  return mount(component, {
    props,
    global: { plugins: [vuetify] },
    attachTo: document.body,
  })
}

// 组装 Config.vue 需要的假接口：表单接口返回 MoviePilot 真实结构
function createApi() {
  return {
    get: vi.fn(async url => {
      if (url.startsWith('plugin/form/')) {
        return { render_mode: 'vue', conf: formFixture.conf, model: formFixture.model }
      }
      if (url.startsWith('system/setting/public/Storages')) {
        return { success: true, data: { value: [{ title: '本地存储', value: 'local', icon: 'mdi-harddisk' }] } }
      }
      throw new Error(`unexpected GET ${url}`)
    }),
    post: vi.fn(async (url, body) => {
      if (url === 'storage/list') {
        expect(body.storage).toBe('local')
        expect(typeof body.path).toBe('string')
        return storageFixture
      }
      throw new Error(`unexpected POST ${url}`)
    }),
  }
}

function findButton(text, root = document) {
  return Array.from(root.querySelectorAll('button')).find(button => button.textContent.includes(text))
}

function findMediaPathTextarea(wrapper) {
  return wrapper.findAll('textarea').find(node => (node.element.placeholder || '').includes('绝对路径'))
}

describe('AiSubV2 Config.vue', () => {
  it('renders the backend JSON form and adds a picker button beside the media path field', async () => {
    const api = createApi()
    const wrapper = mountComponent(Config, {
      api,
      initialConfig: { ...formFixture.model, run_now: true, path_list: '/medias/Movies' },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('媒体路径')
    expect(wrapper.text()).toContain('启用插件')
    expect(findButton('选择文件', wrapper.element)).toBeTruthy()

    const textarea = findMediaPathTextarea(wrapper)
    expect(textarea).toBeTruthy()
    expect(textarea.element.value).toBe('/medias/Movies')

    wrapper.unmount()
  })

  it('saves the whole configuration model through the save event', async () => {
    const api = createApi()
    const wrapper = mountComponent(Config, {
      api,
      initialConfig: { ...formFixture.model, run_now: true, path_list: '/medias/Movies', send_notify: true },
    })
    await flushPromises()

    findButton('保存', wrapper.element).click()
    await flushPromises()

    const saved = wrapper.emitted('save')
    expect(saved).toBeTruthy()
    expect(saved[0][0].path_list).toBe('/medias/Movies')
    expect(saved[0][0].send_notify).toBe(true)
    expect(saved[0][0]).toHaveProperty('llm_model')

    wrapper.unmount()
  })

  it('opens the file manager dialog, hides non-video files and appends the picked paths', async () => {
    const api = createApi()
    const wrapper = mountComponent(Config, {
      api,
      initialConfig: { ...formFixture.model, run_now: true, path_list: '/existing/folder' },
    })
    await flushPromises()

    // 弹窗未打开时不应请求目录列表
    expect(api.post).not.toHaveBeenCalled()

    findButton('选择文件', wrapper.element).click()
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('storage/list', expect.anything(), expect.anything())
    expect(document.body.textContent).toContain('从文件管理器选择视频')
    // 目录与视频文件可见，字幕/文本文件被过滤
    expect(document.body.textContent).toContain('Movies')
    expect(document.body.textContent).toContain('movie.mkv')
    expect(document.body.textContent).toContain('clip.mp4')
    expect(document.body.textContent).not.toContain('notes.txt')
    expect(document.body.textContent).not.toContain('subtitle.srt')

    const row = Array.from(document.querySelectorAll('tr'))
      .find(node => node.textContent.includes('movie.mkv'))
    expect(row).toBeTruthy()
    row.querySelector('input[type="checkbox"]').click()
    await flushPromises()

    findButton('添加所选').click()
    await flushPromises()

    const textarea = findMediaPathTextarea(wrapper)
    expect(textarea.element.value).toBe('/existing/folder\n/medias/movie.mkv')

    wrapper.unmount()
  })

  it('falls back to the current folder when the dialog is asked for a whole directory', async () => {
    const api = createApi()
    const wrapper = mountComponent(Config, {
      api,
      initialConfig: { ...formFixture.model, run_now: true, path_list: '' },
    })
    await flushPromises()

    findButton('选择文件', wrapper.element).click()
    await flushPromises()

    findButton('添加当前文件夹').click()
    await flushPromises()

    const textarea = findMediaPathTextarea(wrapper)
    expect(textarea.element.value).toBe('/')

    wrapper.unmount()
  })
})

describe('AiSubV2 Page.vue', () => {
  it('renders the task table and status summary from the /tasks API', async () => {
    const api = {
      get: vi.fn(async url => {
        expect(url).toBe('plugin/AiSubV2/tasks')
        return {
          success: true,
          data: {
            running: true,
            summary: { total: 2, pending: 0, in_progress: 1, completed: 1, ignored: 0, failed: 0 },
            tasks: [
              {
                task_id: 'a',
                video_file: '/medias/Movies/a.mkv',
                source_label: '手动添加',
                status: 'completed',
                status_text: '已完成',
                add_time: '2026-01-01 10:00:00',
                complete_time: '2026-01-01 10:05:00',
              },
              {
                task_id: 'b',
                video_file: '/medias/TV/b.mkv',
                source_label: '入库触发',
                status: 'in_progress',
                status_text: '处理中',
                add_time: '2026-01-01 10:06:00',
                complete_time: '-',
              },
            ],
          },
        }
      }),
    }

    const wrapper = mountComponent(Page, { api })
    await flushPromises()

    expect(wrapper.text()).toContain('字幕任务')
    expect(wrapper.text()).toContain('服务运行中')
    expect(wrapper.text()).toContain('/medias/Movies/a.mkv')
    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.text()).toContain('入库触发')
    expect(wrapper.text()).toContain('全部 2')

    wrapper.unmount()
  })
})

describe('provider helpers', () => {
  it('classifies video extensions case-insensitively', () => {
    expect(isVideoFile('movie.MKV')).toBe(true)
    expect(isVideoFile('clip.mp4')).toBe(true)
    expect(isVideoFile('subtitle.srt')).toBe(false)
    expect(isVideoFile('noextension')).toBe(false)
  })

  it('joins and splits server paths', () => {
    expect(joinPath('/', 'Movies')).toBe('/Movies')
    expect(joinPath('/medias/', 'movie.mkv')).toBe('/medias/movie.mkv')
    expect(parentPath('/medias/Movies/movie.mkv')).toBe('/medias/Movies')
    expect(parentPath('/')).toBe('/')
    expect(baseName('/medias/Movies/')).toBe('Movies')
    expect(splitPathLines(' /a \n\n/b\n/a ')).toEqual(['/a', '/b', '/a'])
  })

  it('formats sizes for the picker table', () => {
    expect(formatSize(0)).toBe('-')
    expect(formatSize(1536)).toBe('1.5 KB')
    expect(formatSize(1610612736)).toBe('1.5 GB')
  })
})
