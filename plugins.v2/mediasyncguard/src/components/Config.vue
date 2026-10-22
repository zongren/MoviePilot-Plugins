<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import JsonFormNode from './JsonFormNode.vue'
import { errorMessage, unwrapResponse } from '../provider'

/**
 * 媒体同步守护配置页：渲染后端 get_form() 返回的 Vuetify JSON 表单。
 * 工具条提供 保存 / 管理页 / 关闭，通过事件交给宿主导航。
 */
const props = defineProps({
  api: { type: Object, default: () => ({}) },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'MediaSyncGuard' },
  nativeSubscribe: { type: Function, default: null },
  showSwitch: { type: Boolean, default: true },
})

const emit = defineEmits(['save', 'close', 'switch'])

const conf = ref([])
const config = reactive({})
const loading = ref(true)
const loadError = ref('')
const loaded = ref(false)

const configShell = computed(() => (loaded.value ? conf.value : []))

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
    loaded.value = true
    applyConfig({ ...(body.model || {}), ...(props.initialConfig || {}) })
  } catch (error) {
    loadError.value = errorMessage(error, '配置加载失败，请点击重试')
  } finally {
    loading.value = false
  }
}

function save() {
  // 实际保存由宿主完成，这里只把完整配置模型交给宿主
  emit('save', { ...config })
}

watch(() => props.initialConfig, value => {
  if (value && Object.keys(value).length) applyConfig({ ...config, ...value })
}, { deep: true })

onMounted(loadForm)
</script>

<template>
  <div class="ms-config">
    <VToolbar density="comfortable" color="transparent">
      <VIcon class="ms-3" icon="mdi-shield-sync-outline" color="primary" />
      <div class="text-h6 ms-3">媒体同步守护</div>
      <VSpacer />
      <VBtn
        v-if="showSwitch"
        variant="text"
        prepend-icon="mdi-view-dashboard-outline"
        @click="emit('switch')"
      >
        管理页
      </VBtn>
      <VBtn color="primary" variant="tonal" prepend-icon="mdi-content-save" @click="save">
        保存
      </VBtn>
      <VBtn icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
    </VToolbar>
    <VDivider />

    <div class="ms-config-shell">
      <VAlert v-if="loadError" type="warning" variant="tonal" density="compact" class="mb-3">
        {{ loadError }}
        <template #append>
          <VBtn size="small" variant="text" @click="loadForm">重试</VBtn>
        </template>
      </VAlert>

      <VProgressLinear v-if="loading" indeterminate color="primary" />

      <template v-else>
        <VAlert
          v-if="!configShell.length && !loadError"
          type="info"
          variant="tonal"
          density="compact"
          class="mb-3"
        >
          暂无可配置表单（后端 get_form() 返回为空），请稍后重试。
          <template #append>
            <VBtn size="small" variant="text" @click="loadForm">重试</VBtn>
          </template>
        </VAlert>

        <JsonFormNode
          v-for="(node, index) in configShell"
          :key="index"
          :config="node"
          :model="config"
        />

        <div class="ms-config-footer">
          <VBtn v-if="showSwitch" variant="text" prepend-icon="mdi-view-dashboard-outline" @click="emit('switch')">
            管理页
          </VBtn>
          <VSpacer />
          <VBtn variant="text" @click="emit('close')">关闭</VBtn>
          <VBtn color="primary" prepend-icon="mdi-content-save" @click="save">保存</VBtn>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.ms-config {
  background: rgb(var(--v-theme-background));
}

.ms-config-shell {
  padding: 18px;
}

.ms-config-footer {
  align-items: center;
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 16px;
  padding-top: 16px;
}
</style>
