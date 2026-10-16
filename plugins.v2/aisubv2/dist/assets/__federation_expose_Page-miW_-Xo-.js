import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, u as unwrapResponse, e as errorMessage } from './provider-C36M4XYT.js';

const {createElementVNode:_createElementVNode,toDisplayString:_toDisplayString,createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "aisub-page" };
const _hoisted_2 = { class: "aisub-page-shell" };
const _hoisted_3 = { class: "aisub-summary" };
const _hoisted_4 = {
  key: 2,
  class: "aisub-empty text-medium-emphasis"
};
const _hoisted_5 = { class: "text-body-2" };
const _hoisted_6 = { class: "text-body-2 aisub-task-file" };
const _hoisted_7 = { class: "text-body-2" };
const _hoisted_8 = { class: "text-body-2" };

const {computed,onBeforeUnmount,onMounted,ref} = await importShared('vue');

/**
 * AiSubV2 数据页：展示字幕任务队列与状态统计。
 */

const _sfc_main = {
  __name: 'Page',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'AiSubV2' },
  showSwitch: { type: Boolean, default: true },
  nativeSubscribe: { type: Function, default: null },
},
  emits: ['close', 'switch', 'action'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;

const tasks = ref([]);
const summary = ref({});
const running = ref(false);
const loading = ref(false);
const error = ref('');
const autoRefresh = ref(true);
let timer = null;

const summaryChips = computed(() => [
  { key: 'total', label: '全部', color: 'primary', value: summary.value.total || 0 },
  { key: 'pending', label: '等待中', color: 'info', value: summary.value.pending || 0 },
  { key: 'in_progress', label: '处理中', color: 'warning', value: summary.value.in_progress || 0 },
  { key: 'completed', label: '已完成', color: 'success', value: summary.value.completed || 0 },
  { key: 'failed', label: '失败', color: 'error', value: summary.value.failed || 0 },
  { key: 'ignored', label: '已忽略', color: 'grey', value: summary.value.ignored || 0 },
]);

const statusColors = {
  pending: 'info',
  in_progress: 'warning',
  completed: 'success',
  ignored: 'grey',
  failed: 'error',
};

async function refresh() {
  loading.value = true;
  error.value = '';
  try {
    const response = await props.api.get(`plugin/${props.pluginId}/tasks`);
    const data = unwrapResponse(response) || {};
    tasks.value = Array.isArray(data.tasks) ? data.tasks : [];
    summary.value = data.summary || {};
    running.value = Boolean(data.running);
  } catch (err) {
    error.value = errorMessage(err, '任务加载失败');
  } finally {
    loading.value = false;
  }
}

function startTimer() {
  stopTimer();
  if (!autoRefresh.value) return
  timer = setInterval(refresh, 5000);
}

function stopTimer() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function onAutoRefreshChanged(value) {
  autoRefresh.value = Boolean(value);
  startTimer();
}

onMounted(() => {
  refresh();
  startTimer();
});

onBeforeUnmount(stopTimer);

return (_ctx, _cache) => {
  const _component_VChip = _resolveComponent("VChip");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VSwitch = _resolveComponent("VSwitch");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VToolbar = _resolveComponent("VToolbar");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");
  const _component_VTable = _resolveComponent("VTable");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createVNode(_component_VToolbar, {
      density: "comfortable",
      color: "transparent"
    }, {
      default: _withCtx(() => [
        _cache[4] || (_cache[4] = _createElementVNode("div", { class: "text-h6 ms-3" }, "字幕任务", -1)),
        _createVNode(_component_VChip, {
          class: "ms-3",
          size: "small",
          color: running.value ? 'success' : 'grey',
          variant: "tonal"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(running.value ? '服务运行中' : '服务未运行'), 1)
          ]),
          _: 1
        }, 8, ["color"]),
        _createVNode(_component_VSpacer),
        _createVNode(_component_VSwitch, {
          "model-value": autoRefresh.value,
          label: "自动刷新",
          density: "compact",
          "hide-details": "",
          color: "primary",
          class: "me-3",
          "onUpdate:modelValue": onAutoRefreshChanged
        }, null, 8, ["model-value"]),
        _createVNode(_component_VBtn, {
          variant: "text",
          "prepend-icon": "mdi-refresh",
          loading: loading.value,
          onClick: refresh
        }, {
          default: _withCtx(() => [...(_cache[2] || (_cache[2] = [
            _createTextVNode("刷新", -1)
          ]))]),
          _: 1
        }, 8, ["loading"]),
        (__props.showSwitch)
          ? (_openBlock(), _createBlock(_component_VBtn, {
              key: 0,
              variant: "text",
              "prepend-icon": "mdi-cog",
              onClick: _cache[0] || (_cache[0] = $event => (emit('switch')))
            }, {
              default: _withCtx(() => [...(_cache[3] || (_cache[3] = [
                _createTextVNode("配置", -1)
              ]))]),
              _: 1
            }))
          : _createCommentVNode("", true),
        _createVNode(_component_VBtn, {
          icon: "mdi-close",
          variant: "text",
          onClick: _cache[1] || (_cache[1] = $event => (emit('close')))
        })
      ]),
      _: 1
    }),
    _createVNode(_component_VDivider),
    _createElementVNode("div", _hoisted_2, [
      _createElementVNode("div", _hoisted_3, [
        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(summaryChips.value, (chip) => {
          return (_openBlock(), _createBlock(_component_VChip, {
            key: chip.key,
            color: chip.color,
            variant: "tonal",
            size: "small"
          }, {
            default: _withCtx(() => [
              _createTextVNode(_toDisplayString(chip.label) + " " + _toDisplayString(chip.value), 1)
            ]),
            _: 2
          }, 1032, ["color"]))
        }), 128))
      ]),
      (error.value)
        ? (_openBlock(), _createBlock(_component_VAlert, {
            key: 0,
            type: "warning",
            variant: "tonal",
            density: "compact",
            class: "mb-3",
            text: error.value
          }, null, 8, ["text"]))
        : _createCommentVNode("", true),
      (loading.value && !tasks.value.length)
        ? (_openBlock(), _createBlock(_component_VProgressLinear, {
            key: 1,
            indeterminate: "",
            color: "primary"
          }))
        : (!tasks.value.length)
          ? (_openBlock(), _createElementBlock("div", _hoisted_4, "暂无字幕任务"))
          : (_openBlock(), _createBlock(_component_VTable, {
              key: 3,
              density: "compact",
              hover: "",
              class: "aisub-task-table"
            }, {
              default: _withCtx(() => [
                _cache[5] || (_cache[5] = _createElementVNode("thead", null, [
                  _createElementVNode("tr", null, [
                    _createElementVNode("th", { class: "text-start ps-4" }, "添加时间"),
                    _createElementVNode("th", { class: "text-start" }, "视频文件"),
                    _createElementVNode("th", { class: "text-start" }, "来源"),
                    _createElementVNode("th", { class: "text-start" }, "完成时间"),
                    _createElementVNode("th", { class: "text-start" }, "状态")
                  ])
                ], -1)),
                _createElementVNode("tbody", null, [
                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(tasks.value, (task) => {
                    return (_openBlock(), _createElementBlock("tr", {
                      key: task.task_id
                    }, [
                      _createElementVNode("td", _hoisted_5, _toDisplayString(task.add_time), 1),
                      _createElementVNode("td", _hoisted_6, _toDisplayString(task.video_file), 1),
                      _createElementVNode("td", _hoisted_7, _toDisplayString(task.source_label), 1),
                      _createElementVNode("td", _hoisted_8, _toDisplayString(task.complete_time), 1),
                      _createElementVNode("td", null, [
                        _createVNode(_component_VChip, {
                          color: statusColors[task.status] || 'grey',
                          variant: "tonal",
                          size: "small"
                        }, {
                          default: _withCtx(() => [
                            _createTextVNode(_toDisplayString(task.status_text), 1)
                          ]),
                          _: 2
                        }, 1032, ["color"])
                      ])
                    ]))
                  }), 128))
                ])
              ]),
              _: 1
            }))
    ])
  ]))
}
}

};
const Page = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-5b129cec"]]);

export { Page as default };
