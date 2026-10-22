import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, u as unwrapResponse, e as errorMessage } from './_plugin-vue_export-helper-CCjVt3HB.js';

const {toDisplayString:_toDisplayString$1,createTextVNode:_createTextVNode$1,Fragment:_Fragment$1,openBlock:_openBlock$1,createElementBlock:_createElementBlock$1,createCommentVNode:_createCommentVNode$1,renderList:_renderList$1,resolveComponent:_resolveComponent$1,createBlock:_createBlock$1,resolveDynamicComponent:_resolveDynamicComponent,normalizeProps:_normalizeProps,guardReactiveProps:_guardReactiveProps,withCtx:_withCtx$1} = await importShared('vue');


const {computed: computed$1} = await importShared('vue');


/**
 * 渲染后端 get_form() 返回的 Vuetify JSON 节点。
 * 与宿主 FormRender 行为保持一致：支持 model / v-show / v-model:xxx / {{表达式}} / onXxx 事件。
 */

const _sfc_main$1 = {
  __name: 'JsonFormNode',
  props: {
  config: { type: Object, required: true },
  model: { type: Object, required: true },
},
  setup(__props) {

const props = __props;

const node = computed$1(() => props.config || {});
const children = computed$1(() => (Array.isArray(node.value.content) ? node.value.content : []));
const hasText = computed$1(() => node.value.text !== undefined && node.value.text !== null);
const componentName = computed$1(() => node.value.component || 'div');

// 以 model 为作用域求值 {{...}} 表达式或 v-show 条件
function evaluate(expression) {
  try {
    // eslint-disable-next-line no-new-func
    return new Function('model', `with(model){ return (${expression}) }`)(props.model)
  } catch (error) {
    console.error('JSON 表单表达式执行失败:', expression, error);
    return undefined
  }
}

function buildProps(rawProps) {
  const result = {};
  const isExpression = value => typeof value === 'string'
    && value.startsWith('{{') && value.endsWith('}}');
  for (const [key, value] of Object.entries(rawProps || {})) {
    if (key.toLowerCase() === 'modelvalue') {
      result.value = props.model[value];
      result['onUpdate:value'] = next => { props.model[value] = next; };
    } else if (key === 'model' || key === 'v-model') {
      result.modelValue = props.model[value];
      result['onUpdate:modelValue'] = next => { props.model[value] = next; };
    } else if (key === 'show' || key === 'v-show') {
      const visible = evaluate(value);
      result.style = { ...(result.style || {}), display: visible ? '' : 'none' };
    } else if (key.startsWith('model:')) {
      const target = key.slice('model:'.length);
      result[target] = props.model[value];
      result[`onUpdate:${target}`] = next => { props.model[value] = next; };
    } else if (key.startsWith('on') && typeof value === 'string') {
      result[key] = event => {
        try {
          // eslint-disable-next-line no-new-func
          return new Function('model', 'event', `with(model){ return (${value})(event) }`)(
            props.model, event,
          )
        } catch (error) {
          console.error('JSON 表单事件执行失败:', key, error);
        }
      };
    } else if (isExpression(value)) {
      result[key] = evaluate(value.slice(2, -2).trim());
    } else if (typeof value === 'string' && Object.prototype.hasOwnProperty.call(props.model, value)) {
      result[key] = props.model[value];
    } else {
      result[key] = value;
    }
  }
  return result
}

const boundProps = computed$1(() => buildProps(node.value.props));
const textContent = computed$1(() => (
  typeof node.value.text === 'string' ? node.value.text : props.model[node.value.text]
));

return (_ctx, _cache) => {
  const _component_JsonFormNode = _resolveComponent$1("JsonFormNode", true);

  return (_openBlock$1(), _createBlock$1(_resolveDynamicComponent(componentName.value), _normalizeProps(_guardReactiveProps(boundProps.value)), {
    default: _withCtx$1(() => [
      (hasText.value)
        ? (_openBlock$1(), _createElementBlock$1(_Fragment$1, { key: 0 }, [
            _createTextVNode$1(_toDisplayString$1(textContent.value), 1)
          ], 64))
        : (children.value.length)
          ? (_openBlock$1(true), _createElementBlock$1(_Fragment$1, { key: 1 }, _renderList$1(children.value, (child, index) => {
              return (_openBlock$1(), _createBlock$1(_component_JsonFormNode, {
                key: index,
                config: child,
                model: __props.model
              }, null, 8, ["config", "model"]))
            }), 128))
          : _createCommentVNode$1("", true)
    ]),
    _: 1
  }, 16))
}
}

};

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,createTextVNode:_createTextVNode,withCtx:_withCtx,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,toDisplayString:_toDisplayString,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "ms-config" };
const _hoisted_2 = { class: "ms-config-shell" };
const _hoisted_3 = { class: "ms-config-footer" };

const {computed,onMounted,reactive,ref,watch} = await importShared('vue');

/**
 * 媒体同步守护配置页：渲染后端 get_form() 返回的 Vuetify JSON 表单。
 * 工具条提供 保存 / 管理页 / 关闭，通过事件交给宿主导航。
 */

const _sfc_main = {
  __name: 'Config',
  props: {
  api: { type: Object, default: () => ({}) },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'MediaSyncGuard' },
  nativeSubscribe: { type: Function, default: null },
  showSwitch: { type: Boolean, default: true },
},
  emits: ['save', 'close', 'switch'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;

const conf = ref([]);
const config = reactive({});
const loading = ref(true);
const loadError = ref('');
const loaded = ref(false);

const configShell = computed(() => (loaded.value ? conf.value : []));

function applyConfig(model) {
  for (const key of Object.keys(config)) delete config[key];
  Object.assign(config, model || {});
}

async function loadForm() {
  loading.value = true;
  loadError.value = '';
  try {
    const response = await props.api.get(`plugin/form/${props.pluginId}`);
    const body = unwrapResponse(response) || {};
    const schema = Array.isArray(body.conf) ? body.conf : [];
    conf.value = schema;
    loaded.value = true;
    applyConfig({ ...(body.model || {}), ...(props.initialConfig || {}) });
  } catch (error) {
    loadError.value = errorMessage(error, '配置加载失败，请点击重试');
  } finally {
    loading.value = false;
  }
}

function save() {
  // 实际保存由宿主完成，这里只把完整配置模型交给宿主
  emit('save', { ...config });
}

watch(() => props.initialConfig, value => {
  if (value && Object.keys(value).length) applyConfig({ ...config, ...value });
}, { deep: true });

onMounted(loadForm);

return (_ctx, _cache) => {
  const _component_VIcon = _resolveComponent("VIcon");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VToolbar = _resolveComponent("VToolbar");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createVNode(_component_VToolbar, {
      density: "comfortable",
      color: "transparent"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VIcon, {
          class: "ms-3",
          icon: "mdi-shield-sync-outline",
          color: "primary"
        }),
        _cache[6] || (_cache[6] = _createElementVNode("div", { class: "text-h6 ms-3" }, "媒体同步守护", -1)),
        _createVNode(_component_VSpacer),
        (__props.showSwitch)
          ? (_openBlock(), _createBlock(_component_VBtn, {
              key: 0,
              variant: "text",
              "prepend-icon": "mdi-view-dashboard-outline",
              onClick: _cache[0] || (_cache[0] = $event => (emit('switch')))
            }, {
              default: _withCtx(() => [...(_cache[4] || (_cache[4] = [
                _createTextVNode(" 管理页 ", -1)
              ]))]),
              _: 1
            }))
          : _createCommentVNode("", true),
        _createVNode(_component_VBtn, {
          color: "primary",
          variant: "tonal",
          "prepend-icon": "mdi-content-save",
          onClick: save
        }, {
          default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
            _createTextVNode(" 保存 ", -1)
          ]))]),
          _: 1
        }),
        _createVNode(_component_VBtn, {
          icon: "mdi-close",
          variant: "text",
          "aria-label": "关闭",
          onClick: _cache[1] || (_cache[1] = $event => (emit('close')))
        })
      ]),
      _: 1
    }),
    _createVNode(_component_VDivider),
    _createElementVNode("div", _hoisted_2, [
      (loadError.value)
        ? (_openBlock(), _createBlock(_component_VAlert, {
            key: 0,
            type: "warning",
            variant: "tonal",
            density: "compact",
            class: "mb-3"
          }, {
            append: _withCtx(() => [
              _createVNode(_component_VBtn, {
                size: "small",
                variant: "text",
                onClick: loadForm
              }, {
                default: _withCtx(() => [...(_cache[7] || (_cache[7] = [
                  _createTextVNode("重试", -1)
                ]))]),
                _: 1
              })
            ]),
            default: _withCtx(() => [
              _createTextVNode(_toDisplayString(loadError.value) + " ", 1)
            ]),
            _: 1
          }))
        : _createCommentVNode("", true),
      (loading.value)
        ? (_openBlock(), _createBlock(_component_VProgressLinear, {
            key: 1,
            indeterminate: "",
            color: "primary"
          }))
        : (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [
            (!configShell.value.length && !loadError.value)
              ? (_openBlock(), _createBlock(_component_VAlert, {
                  key: 0,
                  type: "info",
                  variant: "tonal",
                  density: "compact",
                  class: "mb-3"
                }, {
                  append: _withCtx(() => [
                    _createVNode(_component_VBtn, {
                      size: "small",
                      variant: "text",
                      onClick: loadForm
                    }, {
                      default: _withCtx(() => [...(_cache[8] || (_cache[8] = [
                        _createTextVNode("重试", -1)
                      ]))]),
                      _: 1
                    })
                  ]),
                  default: _withCtx(() => [
                    _cache[9] || (_cache[9] = _createTextVNode(" 暂无可配置表单（后端 get_form() 返回为空），请稍后重试。 ", -1))
                  ]),
                  _: 1
                }))
              : _createCommentVNode("", true),
            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(configShell.value, (node, index) => {
              return (_openBlock(), _createBlock(_sfc_main$1, {
                key: index,
                config: node,
                model: config
              }, null, 8, ["config", "model"]))
            }), 128)),
            _createElementVNode("div", _hoisted_3, [
              (__props.showSwitch)
                ? (_openBlock(), _createBlock(_component_VBtn, {
                    key: 0,
                    variant: "text",
                    "prepend-icon": "mdi-view-dashboard-outline",
                    onClick: _cache[2] || (_cache[2] = $event => (emit('switch')))
                  }, {
                    default: _withCtx(() => [...(_cache[10] || (_cache[10] = [
                      _createTextVNode(" 管理页 ", -1)
                    ]))]),
                    _: 1
                  }))
                : _createCommentVNode("", true),
              _createVNode(_component_VSpacer),
              _createVNode(_component_VBtn, {
                variant: "text",
                onClick: _cache[3] || (_cache[3] = $event => (emit('close')))
              }, {
                default: _withCtx(() => [...(_cache[11] || (_cache[11] = [
                  _createTextVNode("关闭", -1)
                ]))]),
                _: 1
              }),
              _createVNode(_component_VBtn, {
                color: "primary",
                "prepend-icon": "mdi-content-save",
                onClick: save
              }, {
                default: _withCtx(() => [...(_cache[12] || (_cache[12] = [
                  _createTextVNode("保存", -1)
                ]))]),
                _: 1
              })
            ])
          ], 64))
    ])
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-1bc1697f"]]);

export { Config as default };
