<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  baseName,
  errorMessage,
  formatSize,
  formatTime,
  isVideoFile,
  joinPath,
  parentPath,
  unwrapResponse,
} from '../provider'

/**
 * MoviePilot 文件管理器风格的选择弹窗：
 * 通过 /storage/list 浏览服务端目录，勾选视频文件（或整个文件夹）后回填到配置项。
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  api: { type: Object, default: () => ({}) },
  initialPath: { type: String, default: '' },
  storage: { type: String, default: 'local' },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const storages = ref([{ title: '本地存储', value: 'local', icon: 'mdi-harddisk' }])
const activeStorage = ref(props.storage || 'local')
const cwd = ref('/')
const cwdInput = ref('/')
const entries = ref([])
const loading = ref(false)
const error = ref('')
const selected = ref([])
const keyword = ref('')
const videoOnly = ref(true)
const isSmall = ref(false)

const visibleEntries = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  return entries.value.filter(item => {
    if (!item || !item.name) return false
    const isDir = item.type === 'dir'
    if (!isDir && videoOnly.value && !isVideoFile(item.name)) return false
    if (text && !item.name.toLowerCase().includes(text)) return false
    return true
  })
})

const crumbs = computed(() => {
  const segments = String(cwd.value || '/').split('/').filter(Boolean)
  const list = [{ title: '根目录', path: '/' }]
  let current = ''
  for (const segment of segments) {
    current += `/${segment}`
    list.push({ title: segment, path: current })
  }
  return list
})

const selectedCount = computed(() => selected.value.length)

function close() {
  emit('update:modelValue', false)
}

function itemPath(item) {
  return item?.path || joinPath(cwd.value, item?.name || '')
}

// 读取指定目录，失败时抛出异常由调用方处理
async function listDirectory(path) {
  const target = path || '/'
  const body = {
    storage: activeStorage.value,
    path: target,
    type: 'dir',
    name: baseName(target) || '/',
    basename: baseName(target) || '/',
    children: [],
  }
  const response = await props.api.post('storage/list', body, { params: { sort: 'name' } })
  const data = unwrapResponse(response)
  return Array.isArray(data) ? data : []
}

// 依次尝试目标路径、其父目录和根目录，保证弹窗总能打开一个可用目录
async function openAt(path) {
  loading.value = true
  error.value = ''
  const target = String(path || '').trim()
  const candidates = target ? [target, parentPath(target), '/'] : ['/']
  for (const candidate of candidates) {
    try {
      const list = await listDirectory(candidate)
      entries.value = list
      cwd.value = candidate
      cwdInput.value = candidate
      loading.value = false
      return
    } catch (err) {
      error.value = errorMessage(err, '目录读取失败')
    }
  }
  entries.value = []
  loading.value = false
  if (!error.value) error.value = '目录读取失败'
}

async function loadStorages() {
  try {
    const response = await props.api.get('system/setting/public/Storages')
    const data = unwrapResponse(response)
    const list = Array.isArray(data) ? data : (Array.isArray(data?.value) ? data.value : [])
    const normalized = list
      .map(item => ({
        title: item?.title || item?.name || item?.value || String(item?.type || ''),
        value: item?.value || item?.name || item?.type || '',
        icon: item?.icon || 'mdi-harddisk',
      }))
      .filter(item => item.value)
    if (normalized.length) storages.value = normalized
  } catch (err) {
    // 读取存储列表失败时保持本地存储兜底，不影响浏览
  }
}

function enterEntry(item) {
  if (item?.type !== 'dir') return
  openAt(itemPath(item))
}

function toggleEntry(item) {
  const path = itemPath(item)
  if (!path) return
  const index = selected.value.indexOf(path)
  if (index >= 0) selected.value.splice(index, 1)
  else selected.value.push(path)
}

function isSelected(item) {
  return selected.value.includes(itemPath(item))
}

function selectVisible() {
  for (const item of visibleEntries.value) {
    const path = itemPath(item)
    if (path && !selected.value.includes(path)) selected.value.push(path)
  }
}

function clearSelection() {
  selected.value = []
}

function confirmSelection() {
  if (!selected.value.length) return
  emit('confirm', selected.value.slice())
  close()
}

function addCurrentFolder() {
  emit('confirm', [cwd.value])
  close()
}

function onStorageChanged(value) {
  activeStorage.value = value
  selected.value = []
  openAt('/')
}

function updateSmallScreen() {
  isSmall.value = window.innerWidth < 720
}

watch(() => props.modelValue, opened => {
  if (!opened) return
  selected.value = []
  keyword.value = ''
  updateSmallScreen()
  activeStorage.value = props.storage || 'local'
  loadStorages()
  openAt(props.initialPath)
})

onMounted(updateSmallScreen)
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="isSmall"
    max-width="72rem"
    scrollable
    @update:model-value="value => emit('update:modelValue', value)"
  >
    <VCard class="aisub-file-picker">
      <VToolbar color="primary" density="comfortable">
        <VToolbarTitle>从文件管理器选择视频</VToolbarTitle>
        <VSpacer />
        <VBtn icon="mdi-close" variant="text" @click="close" />
      </VToolbar>

      <VCardText class="aisub-picker-toolbar">
        <div class="aisub-picker-nav">
          <VSelect
            :model-value="activeStorage"
            :items="storages"
            item-title="title"
            item-value="value"
            density="compact"
            hide-details
            class="aisub-storage-select"
            label="存储"
            @update:model-value="onStorageChanged"
          />
          <VBtn icon="mdi-arrow-up" variant="text" title="上级目录" @click="openAt(parentPath(cwd))" />
          <VBtn icon="mdi-refresh" variant="text" title="刷新" @click="openAt(cwd)" />
        </div>

        <div class="aisub-picker-crumbs">
          <template v-for="(crumb, index) in crumbs" :key="crumb.path">
            <VBtn
              variant="text"
              size="small"
              class="text-none"
              :disabled="index === crumbs.length - 1"
              @click="openAt(crumb.path)"
            >
              {{ crumb.title }}
            </VBtn>
            <VIcon v-if="index < crumbs.length - 1" size="14" icon="mdi-chevron-right" />
          </template>
        </div>

        <VTextField
          v-model="cwdInput"
          density="compact"
          hide-details
          label="当前目录（可直接输入绝对路径）"
          prepend-inner-icon="mdi-folder-outline"
          @keyup.enter="openAt(cwdInput)"
        >
          <template #append>
            <VBtn icon="mdi-arrow-right" variant="text" size="small" @click="openAt(cwdInput)" />
          </template>
        </VTextField>

        <div class="aisub-picker-filters">
          <VTextField
            v-model="keyword"
            density="compact"
            hide-details
            clearable
            label="按名称过滤"
            prepend-inner-icon="mdi-magnify"
            class="aisub-keyword"
          />
          <VSwitch v-model="videoOnly" label="仅显示视频文件" density="compact" hide-details color="primary" />
        </div>

        <VAlert v-if="error" type="warning" variant="tonal" density="compact" class="mt-2" :text="error" />
      </VCardText>

      <VDivider />

      <VCardText class="aisub-picker-list pa-0">
        <VProgressLinear v-if="loading" indeterminate color="primary" />
        <div v-if="!loading && !visibleEntries.length" class="aisub-picker-empty text-medium-emphasis">
          当前目录没有可选项
        </div>
        <VTable v-else density="compact" hover class="aisub-picker-table">
          <thead>
            <tr>
              <th class="text-start" style="width: 56px;">选择</th>
              <th class="text-start">名称</th>
              <th class="text-start" style="width: 110px;">大小</th>
              <th class="text-start" style="width: 180px;">修改时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in visibleEntries" :key="itemPath(item)">
              <td>
                <VCheckbox
                  :model-value="isSelected(item)"
                  density="compact"
                  hide-details
                  @update:model-value="toggleEntry(item)"
                />
              </td>
              <td>
                <div class="aisub-entry-name" @click="item.type === 'dir' ? enterEntry(item) : toggleEntry(item)">
                  <VIcon
                    size="18"
                    :icon="item.type === 'dir' ? 'mdi-folder' : 'mdi-file-video-outline'"
                    :color="item.type === 'dir' ? 'warning' : 'primary'"
                  />
                  <span class="text-body-2">{{ item.name }}</span>
                  <VBtn
                    v-if="item.type === 'dir'"
                    size="x-small"
                    variant="text"
                    icon="mdi-arrow-right"
                    @click.stop="enterEntry(item)"
                  />
                </div>
              </td>
              <td class="text-body-2">{{ item.type === 'dir' ? '-' : formatSize(item.size) }}</td>
              <td class="text-body-2">{{ formatTime(item.modify_time) }}</td>
            </tr>
          </tbody>
        </VTable>
      </VCardText>

      <VDivider />

      <VCardActions class="aisub-picker-actions">
        <span class="text-body-2">已选 {{ selectedCount }} 项</span>
        <VBtn variant="text" size="small" @click="selectVisible">全选本页</VBtn>
        <VBtn variant="text" size="small" :disabled="!selectedCount" @click="clearSelection">清空</VBtn>
        <VSpacer />
        <VBtn variant="text" prepend-icon="mdi-folder-plus-outline" @click="addCurrentFolder">
          添加当前文件夹
        </VBtn>
        <VBtn variant="text" @click="close">取消</VBtn>
        <VBtn color="primary" prepend-icon="mdi-check" :disabled="!selectedCount" @click="confirmSelection">
          添加所选
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.aisub-picker-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.aisub-picker-nav {
  align-items: center;
  display: flex;
  gap: 8px;
}

.aisub-storage-select {
  max-width: 220px;
}

.aisub-picker-crumbs {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}

.aisub-picker-filters {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.aisub-keyword {
  max-width: 320px;
}

.aisub-picker-list {
  max-height: 55vh;
  min-height: 180px;
  overflow: auto;
}

.aisub-picker-empty {
  padding: 40px 16px;
  text-align: center;
}

.aisub-entry-name {
  align-items: center;
  cursor: pointer;
  display: flex;
  gap: 6px;
}

.aisub-picker-actions {
  gap: 8px;
}
</style>
