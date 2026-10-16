<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { errorMessage, unwrapResponse } from '../provider'

/**
 * AiSubV2 数据页：展示字幕任务队列与状态统计。
 */
const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'AiSubV2' },
  showSwitch: { type: Boolean, default: true },
  nativeSubscribe: { type: Function, default: null },
})

const emit = defineEmits(['close', 'switch', 'action'])

const tasks = ref([])
const summary = ref({})
const running = ref(false)
const loading = ref(false)
const error = ref('')
const autoRefresh = ref(true)
let timer = null

const summaryChips = computed(() => [
  { key: 'total', label: '全部', color: 'primary', value: summary.value.total || 0 },
  { key: 'pending', label: '等待中', color: 'info', value: summary.value.pending || 0 },
  { key: 'in_progress', label: '处理中', color: 'warning', value: summary.value.in_progress || 0 },
  { key: 'completed', label: '已完成', color: 'success', value: summary.value.completed || 0 },
  { key: 'failed', label: '失败', color: 'error', value: summary.value.failed || 0 },
  { key: 'ignored', label: '已忽略', color: 'grey', value: summary.value.ignored || 0 },
])

const statusColors = {
  pending: 'info',
  in_progress: 'warning',
  completed: 'success',
  ignored: 'grey',
  failed: 'error',
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const response = await props.api.get(`plugin/${props.pluginId}/tasks`)
    const data = unwrapResponse(response) || {}
    tasks.value = Array.isArray(data.tasks) ? data.tasks : []
    summary.value = data.summary || {}
    running.value = Boolean(data.running)
  } catch (err) {
    error.value = errorMessage(err, '任务加载失败')
  } finally {
    loading.value = false
  }
}

function startTimer() {
  stopTimer()
  if (!autoRefresh.value) return
  timer = setInterval(refresh, 5000)
}

function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

function onAutoRefreshChanged(value) {
  autoRefresh.value = Boolean(value)
  startTimer()
}

onMounted(() => {
  refresh()
  startTimer()
})

onBeforeUnmount(stopTimer)
</script>

<template>
  <div class="aisub-page">
    <VToolbar density="comfortable" color="transparent">
      <div class="text-h6 ms-3">字幕任务</div>
      <VChip
        class="ms-3"
        size="small"
        :color="running ? 'success' : 'grey'"
        variant="tonal"
      >
        {{ running ? '服务运行中' : '服务未运行' }}
      </VChip>
      <VSpacer />
      <VSwitch
        :model-value="autoRefresh"
        label="自动刷新"
        density="compact"
        hide-details
        color="primary"
        class="me-3"
        @update:model-value="onAutoRefreshChanged"
      />
      <VBtn variant="text" prepend-icon="mdi-refresh" :loading="loading" @click="refresh">刷新</VBtn>
      <VBtn v-if="showSwitch" variant="text" prepend-icon="mdi-cog" @click="emit('switch')">配置</VBtn>
      <VBtn icon="mdi-close" variant="text" @click="emit('close')" />
    </VToolbar>
    <VDivider />

    <div class="aisub-page-shell">
      <div class="aisub-summary">
        <VChip
          v-for="chip in summaryChips"
          :key="chip.key"
          :color="chip.color"
          variant="tonal"
          size="small"
        >
          {{ chip.label }} {{ chip.value }}
        </VChip>
      </div>

      <VAlert v-if="error" type="warning" variant="tonal" density="compact" class="mb-3" :text="error" />

      <VProgressLinear v-if="loading && !tasks.length" indeterminate color="primary" />

      <div v-else-if="!tasks.length" class="aisub-empty text-medium-emphasis">暂无字幕任务</div>

      <VTable v-else density="compact" hover class="aisub-task-table">
        <thead>
          <tr>
            <th class="text-start ps-4">添加时间</th>
            <th class="text-start">视频文件</th>
            <th class="text-start">来源</th>
            <th class="text-start">完成时间</th>
            <th class="text-start">状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.task_id">
            <td class="text-body-2">{{ task.add_time }}</td>
            <td class="text-body-2 aisub-task-file">{{ task.video_file }}</td>
            <td class="text-body-2">{{ task.source_label }}</td>
            <td class="text-body-2">{{ task.complete_time }}</td>
            <td>
              <VChip :color="statusColors[task.status] || 'grey'" variant="tonal" size="small">
                {{ task.status_text }}
              </VChip>
            </td>
          </tr>
        </tbody>
      </VTable>
    </div>
  </div>
</template>

<style scoped>
.aisub-page {
  background: rgb(var(--v-theme-background));
}

.aisub-page-shell {
  padding: 18px;
}

.aisub-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.aisub-empty {
  padding: 40px 16px;
  text-align: center;
}

.aisub-task-file {
  max-width: 420px;
  overflow-wrap: anywhere;
}
</style>
