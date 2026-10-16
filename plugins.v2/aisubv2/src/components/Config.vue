<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import JsonFormNode from './JsonFormNode.vue'
import FilePickerDialog from './FilePickerDialog.vue'
import { baseName, errorMessage, isVideoFile, parentPath, splitPathLines, unwrapResponse } from '../provider'

/**
 * AiSubV2 配置页：复用后端 get_form() 的 Vuetify JSON 表单，
 * 并为媒体路径补充“从文件管理器选择”弹窗。
 */
const props = defineProps({
  api: { type: Object, default: () => ({}) },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'AiSubV2' },
  nativeSubscribe: { type: Function, default: null },
  showSwitch: { type: Boolean, default: true },
})

const emit = defineEmits(['save', 'close', 'switch'])

const conf = ref([])
const config = reactive({})
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const pickerOpen = ref(false)
const pickerKey = ref('path_list')

// 选择弹窗的起始目录：优先使用已填写的第一个路径
const pickerInitialPath = computed(() => {
  const key = pickerKey.value || 'path_list'
  const first = splitPathLines(config[key])[0] || ''
  if (!first) return ''
  const name = baseName(first)
  return isVideoFile(name) ? parentPath(first) : first
})

function applyConfig(model) {
  for (const key of Object.keys(config)) delete config[key]
  Object.assign(config, model || {})
}

async function loadForm() {
  loading.value = true
  loadError.value = ''
  try {
    const response = await props.api.get(`plugin/form/${props.pluginId}`)
    const body = unwrapResponse(response) || {}
    const schema = Array.isArray(body.conf) ? body.conf : []
    conf.value = schema
    applyConfig({ ...(body.model || {}), ...(props.initialConfig || {}) })
    if (!schema.length) loadError.value = '配置表单加载为空，请点击重试'
  } catch (error) {
    loadError.value = errorMessage(error, '配置加载失败，请点击重试')
  } finally {
    loading.value = false
  }
}

function save() {
  saving.value = true
  try {
    emit('save', { ...config })
  } finally {
    saving.value = false
  }
}

function openPicker(modelKey) {
  pickerKey.value = typeof modelKey === 'string' && modelKey ? modelKey : 'path_list'
  pickerOpen.value = true
}

// 将弹窗中选择的路径追加到目标配置项（自动去重）
function appendPickedPaths(paths) {
  const key = pickerKey.value || 'path_list'
  const merged = splitPathLines(config[key])
  for (const path of paths || []) {
    const value = String(path || '').trim()
    if (value && !merged.includes(value)) merged.push(value)
  }
  config[key] = merged.join('\n')
}

watch(() => props.initialConfig, value => {
  if (value && Object.keys(value).length) applyConfig({ ...config, ...value })
}, { deep: true })

onMounted(loadForm)
</script>

<template>
  <div class="aisub-config">
    <VToolbar density="comfortable" color="transparent">
      <div class="text-h6 ms-3">AI字幕生成配置</div>
      <VSpacer />
      <VBtn variant="text" prepend-icon="mdi-format-list-bulleted" @click="emit('switch')">查看任务</VBtn>
      <VBtn color="primary" variant="tonal" prepend-icon="mdi-content-save" :loading="saving" @click="save">
        保存
      </VBtn>
      <VBtn icon="mdi-close" variant="text" @click="emit('close')" />
    </VToolbar>
    <VDivider />

    <div class="aisub-config-shell">
      <VAlert v-if="loadError" type="warning" variant="tonal" density="compact" class="mb-3">
        {{ loadError }}
        <template #append>
          <VBtn size="small" variant="text" @click="loadForm">重试</VBtn>
        </template>
      </VAlert>

      <VProgressLinear v-if="loading" indeterminate color="primary" />

      <template v-else>
        <JsonFormNode
          v-for="(node, index) in conf"
          :key="index"
          :config="node"
          :model="config"
          :on-pick="openPicker"
        />

        <div class="aisub-config-footer">
          <VBtn variant="text" prepend-icon="mdi-format-list-bulleted" @click="emit('switch')">查看任务</VBtn>
          <VSpacer />
          <VBtn variant="text" @click="emit('close')">关闭</VBtn>
          <VBtn color="primary" prepend-icon="mdi-content-save" :loading="saving" @click="save">保存</VBtn>
        </div>
      </template>
    </div>

    <FilePickerDialog
      v-model="pickerOpen"
      :api="api"
      :initial-path="pickerInitialPath"
      @confirm="appendPickedPaths"
    />
  </div>
</template>

<style scoped>
.aisub-config {
  background: rgb(var(--v-theme-background));
}

.aisub-config-shell {
  padding: 18px;
}

.aisub-config-footer {
  align-items: center;
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  display: flex;
  gap: 10px;
  padding-top: 16px;
}
</style>
