import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc, i as isVideoFile, p as parentPath, f as formatSize, a as formatTime, u as unwrapResponse, e as errorMessage, j as joinPath, b as baseName, s as splitPathLines } from './provider-C36M4XYT.js';

const {resolveDynamicComponent:_resolveDynamicComponent,mergeProps:_mergeProps,openBlock:_openBlock$2,createBlock:_createBlock$2,createTextVNode:_createTextVNode$2,resolveComponent:_resolveComponent$2,withCtx:_withCtx$2,createVNode:_createVNode$2,createElementBlock:_createElementBlock$2,createCommentVNode:_createCommentVNode$2,toDisplayString:_toDisplayString$2,Fragment:_Fragment$2,renderList:_renderList$2,normalizeProps:_normalizeProps,guardReactiveProps:_guardReactiveProps} = await importShared('vue');


const _hoisted_1$2 = {
  key: 0,
  class: "aisub-picker-row"
};

const {computed: computed$2} = await importShared('vue');


/**
 * 渲染后端 get_form() 返回的 Vuetify JSON 节点。
 * 与宿主 FormRender 行为保持一致：支持 model / v-show / v-model:xxx / {{表达式}} / onXxx 事件，
 * 并额外识别节点上的 picker 标记，在输入框旁渲染“选择文件”按钮。
 */

const _sfc_main$2 = {
  __name: 'JsonFormNode',
  props: {
  config: { type: Object, required: true },
  model: { type: Object, required: true },
  onPick: { type: Function, default: null },
},
  setup(__props) {

const props = __props;

const node = computed$2(() => props.config || {});
const children = computed$2(() => (Array.isArray(node.value.content) ? node.value.content : []));
const pickerKey = computed$2(() => (typeof node.value.picker === 'string' ? node.value.picker : ''));
const hasText = computed$2(() => node.value.text !== undefined && node.value.text !== null);
const componentName = computed$2(() => node.value.component || 'div');

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

const boundProps = computed$2(() => buildProps(node.value.props));
const textContent = computed$2(() => (
  typeof node.value.text === 'string' ? node.value.text : props.model[node.value.text]
));

return (_ctx, _cache) => {
  const _component_VBtn = _resolveComponent$2("VBtn");
  const _component_JsonFormNode = _resolveComponent$2("JsonFormNode", true);

  return (pickerKey.value)
    ? (_openBlock$2(), _createElementBlock$2("div", _hoisted_1$2, [
        (_openBlock$2(), _createBlock$2(_resolveDynamicComponent(componentName.value), _mergeProps(boundProps.value, { class: "aisub-picker-input" }), null, 16)),
        _createVNode$2(_component_VBtn, {
          class: "aisub-picker-button",
          color: "primary",
          variant: "tonal",
          "prepend-icon": "mdi-folder-search-outline",
          onClick: _cache[0] || (_cache[0] = $event => (__props.onPick && __props.onPick(pickerKey.value)))
        }, {
          default: _withCtx$2(() => [...(_cache[1] || (_cache[1] = [
            _createTextVNode$2(" 选择文件 ", -1)
          ]))]),
          _: 1
        })
      ]))
    : (_openBlock$2(), _createBlock$2(_resolveDynamicComponent(componentName.value), _normalizeProps(_mergeProps({ key: 1 }, boundProps.value)), {
        default: _withCtx$2(() => [
          (hasText.value)
            ? (_openBlock$2(), _createElementBlock$2(_Fragment$2, { key: 0 }, [
                _createTextVNode$2(_toDisplayString$2(textContent.value), 1)
              ], 64))
            : (children.value.length)
              ? (_openBlock$2(true), _createElementBlock$2(_Fragment$2, { key: 1 }, _renderList$2(children.value, (child, index) => {
                  return (_openBlock$2(), _createBlock$2(_component_JsonFormNode, {
                    key: index,
                    config: child,
                    model: __props.model,
                    "on-pick": __props.onPick
                  }, null, 8, ["config", "model", "on-pick"]))
                }), 128))
              : _createCommentVNode$2("", true)
        ]),
        _: 1
      }, 16))
}
}

};
const JsonFormNode = /*#__PURE__*/_export_sfc(_sfc_main$2, [['__scopeId',"data-v-8ffe9390"]]);

const {createTextVNode:_createTextVNode$1,resolveComponent:_resolveComponent$1,withCtx:_withCtx$1,createVNode:_createVNode$1,unref:_unref,createElementVNode:_createElementVNode$1,renderList:_renderList$1,Fragment:_Fragment$1,openBlock:_openBlock$1,createElementBlock:_createElementBlock$1,toDisplayString:_toDisplayString$1,createBlock:_createBlock$1,createCommentVNode:_createCommentVNode$1,withKeys:_withKeys,withModifiers:_withModifiers} = await importShared('vue');


const _hoisted_1$1 = { class: "aisub-picker-nav" };
const _hoisted_2$1 = { class: "aisub-picker-crumbs" };
const _hoisted_3$1 = { class: "aisub-picker-filters" };
const _hoisted_4 = {
  key: 1,
  class: "aisub-picker-empty text-medium-emphasis"
};
const _hoisted_5 = ["onClick"];
const _hoisted_6 = { class: "text-body-2" };
const _hoisted_7 = { class: "text-body-2" };
const _hoisted_8 = { class: "text-body-2" };
const _hoisted_9 = { class: "text-body-2" };

const {computed: computed$1,onMounted: onMounted$1,ref: ref$1,watch: watch$1} = await importShared('vue');

/**
 * MoviePilot 文件管理器风格的选择弹窗：
 * 通过 /storage/list 浏览服务端目录，勾选视频文件（或整个文件夹）后回填到配置项。
 */

const _sfc_main$1 = {
  __name: 'FilePickerDialog',
  props: {
  modelValue: { type: Boolean, default: false },
  api: { type: Object, default: () => ({}) },
  initialPath: { type: String, default: '' },
  storage: { type: String, default: 'local' },
},
  emits: ['update:modelValue', 'confirm'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;

const storages = ref$1([{ title: '本地存储', value: 'local', icon: 'mdi-harddisk' }]);
const activeStorage = ref$1(props.storage || 'local');
const cwd = ref$1('/');
const cwdInput = ref$1('/');
const entries = ref$1([]);
const loading = ref$1(false);
const error = ref$1('');
const selected = ref$1([]);
const keyword = ref$1('');
const videoOnly = ref$1(true);
const isSmall = ref$1(false);

const visibleEntries = computed$1(() => {
  const text = keyword.value.trim().toLowerCase();
  return entries.value.filter(item => {
    if (!item || !item.name) return false
    const isDir = item.type === 'dir';
    if (!isDir && videoOnly.value && !isVideoFile(item.name)) return false
    if (text && !item.name.toLowerCase().includes(text)) return false
    return true
  })
});

const crumbs = computed$1(() => {
  const segments = String(cwd.value || '/').split('/').filter(Boolean);
  const list = [{ title: '根目录', path: '/' }];
  let current = '';
  for (const segment of segments) {
    current += `/${segment}`;
    list.push({ title: segment, path: current });
  }
  return list
});

const selectedCount = computed$1(() => selected.value.length);

function close() {
  emit('update:modelValue', false);
}

function itemPath(item) {
  return item?.path || joinPath(cwd.value, item?.name || '')
}

// 读取指定目录，失败时抛出异常由调用方处理
async function listDirectory(path) {
  const target = path || '/';
  const body = {
    storage: activeStorage.value,
    path: target,
    type: 'dir',
    name: baseName(target) || '/',
    basename: baseName(target) || '/',
    children: [],
  };
  const response = await props.api.post('storage/list', body, { params: { sort: 'name' } });
  const data = unwrapResponse(response);
  return Array.isArray(data) ? data : []
}

// 依次尝试目标路径、其父目录和根目录，保证弹窗总能打开一个可用目录
async function openAt(path) {
  loading.value = true;
  error.value = '';
  const target = String(path || '').trim();
  const candidates = target ? [target, parentPath(target), '/'] : ['/'];
  for (const candidate of candidates) {
    try {
      const list = await listDirectory(candidate);
      entries.value = list;
      cwd.value = candidate;
      cwdInput.value = candidate;
      loading.value = false;
      return
    } catch (err) {
      error.value = errorMessage(err, '目录读取失败');
    }
  }
  entries.value = [];
  loading.value = false;
  if (!error.value) error.value = '目录读取失败';
}

async function loadStorages() {
  try {
    const response = await props.api.get('system/setting/public/Storages');
    const data = unwrapResponse(response);
    const list = Array.isArray(data) ? data : (Array.isArray(data?.value) ? data.value : []);
    const normalized = list
      .map(item => ({
        title: item?.title || item?.name || item?.value || String(item?.type || ''),
        value: item?.value || item?.name || item?.type || '',
        icon: item?.icon || 'mdi-harddisk',
      }))
      .filter(item => item.value);
    if (normalized.length) storages.value = normalized;
  } catch (err) {
    // 读取存储列表失败时保持本地存储兜底，不影响浏览
  }
}

function enterEntry(item) {
  if (item?.type !== 'dir') return
  openAt(itemPath(item));
}

function toggleEntry(item) {
  const path = itemPath(item);
  if (!path) return
  const index = selected.value.indexOf(path);
  if (index >= 0) selected.value.splice(index, 1);
  else selected.value.push(path);
}

function isSelected(item) {
  return selected.value.includes(itemPath(item))
}

function selectVisible() {
  for (const item of visibleEntries.value) {
    const path = itemPath(item);
    if (path && !selected.value.includes(path)) selected.value.push(path);
  }
}

function clearSelection() {
  selected.value = [];
}

function confirmSelection() {
  if (!selected.value.length) return
  emit('confirm', selected.value.slice());
  close();
}

function addCurrentFolder() {
  emit('confirm', [cwd.value]);
  close();
}

function onStorageChanged(value) {
  activeStorage.value = value;
  selected.value = [];
  openAt('/');
}

function updateSmallScreen() {
  isSmall.value = window.innerWidth < 720;
}

watch$1(() => props.modelValue, opened => {
  if (!opened) return
  selected.value = [];
  keyword.value = '';
  updateSmallScreen();
  activeStorage.value = props.storage || 'local';
  loadStorages();
  openAt(props.initialPath);
});

onMounted$1(updateSmallScreen);

return (_ctx, _cache) => {
  const _component_VToolbarTitle = _resolveComponent$1("VToolbarTitle");
  const _component_VSpacer = _resolveComponent$1("VSpacer");
  const _component_VBtn = _resolveComponent$1("VBtn");
  const _component_VToolbar = _resolveComponent$1("VToolbar");
  const _component_VSelect = _resolveComponent$1("VSelect");
  const _component_VIcon = _resolveComponent$1("VIcon");
  const _component_VTextField = _resolveComponent$1("VTextField");
  const _component_VSwitch = _resolveComponent$1("VSwitch");
  const _component_VAlert = _resolveComponent$1("VAlert");
  const _component_VCardText = _resolveComponent$1("VCardText");
  const _component_VDivider = _resolveComponent$1("VDivider");
  const _component_VProgressLinear = _resolveComponent$1("VProgressLinear");
  const _component_VCheckbox = _resolveComponent$1("VCheckbox");
  const _component_VTable = _resolveComponent$1("VTable");
  const _component_VCardActions = _resolveComponent$1("VCardActions");
  const _component_VCard = _resolveComponent$1("VCard");
  const _component_VDialog = _resolveComponent$1("VDialog");

  return (_openBlock$1(), _createBlock$1(_component_VDialog, {
    "model-value": __props.modelValue,
    fullscreen: isSmall.value,
    "max-width": "72rem",
    scrollable: "",
    "onUpdate:modelValue": _cache[7] || (_cache[7] = value => emit('update:modelValue', value))
  }, {
    default: _withCtx$1(() => [
      _createVNode$1(_component_VCard, { class: "aisub-file-picker" }, {
        default: _withCtx$1(() => [
          _createVNode$1(_component_VToolbar, {
            color: "primary",
            density: "comfortable"
          }, {
            default: _withCtx$1(() => [
              _createVNode$1(_component_VToolbarTitle, null, {
                default: _withCtx$1(() => [...(_cache[8] || (_cache[8] = [
                  _createTextVNode$1("从文件管理器选择视频", -1)
                ]))]),
                _: 1
              }),
              _createVNode$1(_component_VSpacer),
              _createVNode$1(_component_VBtn, {
                icon: "mdi-close",
                variant: "text",
                onClick: close
              })
            ]),
            _: 1
          }),
          _createVNode$1(_component_VCardText, { class: "aisub-picker-toolbar" }, {
            default: _withCtx$1(() => [
              _createElementVNode$1("div", _hoisted_1$1, [
                _createVNode$1(_component_VSelect, {
                  "model-value": activeStorage.value,
                  items: storages.value,
                  "item-title": "title",
                  "item-value": "value",
                  density: "compact",
                  "hide-details": "",
                  class: "aisub-storage-select",
                  label: "存储",
                  "onUpdate:modelValue": onStorageChanged
                }, null, 8, ["model-value", "items"]),
                _createVNode$1(_component_VBtn, {
                  icon: "mdi-arrow-up",
                  variant: "text",
                  title: "上级目录",
                  onClick: _cache[0] || (_cache[0] = $event => (openAt(_unref(parentPath)(cwd.value))))
                }),
                _createVNode$1(_component_VBtn, {
                  icon: "mdi-refresh",
                  variant: "text",
                  title: "刷新",
                  onClick: _cache[1] || (_cache[1] = $event => (openAt(cwd.value)))
                })
              ]),
              _createElementVNode$1("div", _hoisted_2$1, [
                (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(crumbs.value, (crumb, index) => {
                  return (_openBlock$1(), _createElementBlock$1(_Fragment$1, {
                    key: crumb.path
                  }, [
                    _createVNode$1(_component_VBtn, {
                      variant: "text",
                      size: "small",
                      class: "text-none",
                      disabled: index === crumbs.value.length - 1,
                      onClick: $event => (openAt(crumb.path))
                    }, {
                      default: _withCtx$1(() => [
                        _createTextVNode$1(_toDisplayString$1(crumb.title), 1)
                      ]),
                      _: 2
                    }, 1032, ["disabled", "onClick"]),
                    (index < crumbs.value.length - 1)
                      ? (_openBlock$1(), _createBlock$1(_component_VIcon, {
                          key: 0,
                          size: "14",
                          icon: "mdi-chevron-right"
                        }))
                      : _createCommentVNode$1("", true)
                  ], 64))
                }), 128))
              ]),
              _createVNode$1(_component_VTextField, {
                modelValue: cwdInput.value,
                "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((cwdInput).value = $event)),
                density: "compact",
                "hide-details": "",
                label: "当前目录（可直接输入绝对路径）",
                "prepend-inner-icon": "mdi-folder-outline",
                onKeyup: _cache[4] || (_cache[4] = _withKeys($event => (openAt(cwdInput.value)), ["enter"]))
              }, {
                append: _withCtx$1(() => [
                  _createVNode$1(_component_VBtn, {
                    icon: "mdi-arrow-right",
                    variant: "text",
                    size: "small",
                    onClick: _cache[2] || (_cache[2] = $event => (openAt(cwdInput.value)))
                  })
                ]),
                _: 1
              }, 8, ["modelValue"]),
              _createElementVNode$1("div", _hoisted_3$1, [
                _createVNode$1(_component_VTextField, {
                  modelValue: keyword.value,
                  "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((keyword).value = $event)),
                  density: "compact",
                  "hide-details": "",
                  clearable: "",
                  label: "按名称过滤",
                  "prepend-inner-icon": "mdi-magnify",
                  class: "aisub-keyword"
                }, null, 8, ["modelValue"]),
                _createVNode$1(_component_VSwitch, {
                  modelValue: videoOnly.value,
                  "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((videoOnly).value = $event)),
                  label: "仅显示视频文件",
                  density: "compact",
                  "hide-details": "",
                  color: "primary"
                }, null, 8, ["modelValue"])
              ]),
              (error.value)
                ? (_openBlock$1(), _createBlock$1(_component_VAlert, {
                    key: 0,
                    type: "warning",
                    variant: "tonal",
                    density: "compact",
                    class: "mt-2",
                    text: error.value
                  }, null, 8, ["text"]))
                : _createCommentVNode$1("", true)
            ]),
            _: 1
          }),
          _createVNode$1(_component_VDivider),
          _createVNode$1(_component_VCardText, { class: "aisub-picker-list pa-0" }, {
            default: _withCtx$1(() => [
              (loading.value)
                ? (_openBlock$1(), _createBlock$1(_component_VProgressLinear, {
                    key: 0,
                    indeterminate: "",
                    color: "primary"
                  }))
                : _createCommentVNode$1("", true),
              (!loading.value && !visibleEntries.value.length)
                ? (_openBlock$1(), _createElementBlock$1("div", _hoisted_4, " 当前目录没有可选项 "))
                : (_openBlock$1(), _createBlock$1(_component_VTable, {
                    key: 2,
                    density: "compact",
                    hover: "",
                    class: "aisub-picker-table"
                  }, {
                    default: _withCtx$1(() => [
                      _cache[9] || (_cache[9] = _createElementVNode$1("thead", null, [
                        _createElementVNode$1("tr", null, [
                          _createElementVNode$1("th", {
                            class: "text-start",
                            style: {"width":"56px"}
                          }, "选择"),
                          _createElementVNode$1("th", { class: "text-start" }, "名称"),
                          _createElementVNode$1("th", {
                            class: "text-start",
                            style: {"width":"110px"}
                          }, "大小"),
                          _createElementVNode$1("th", {
                            class: "text-start",
                            style: {"width":"180px"}
                          }, "修改时间")
                        ])
                      ], -1)),
                      _createElementVNode$1("tbody", null, [
                        (_openBlock$1(true), _createElementBlock$1(_Fragment$1, null, _renderList$1(visibleEntries.value, (item) => {
                          return (_openBlock$1(), _createElementBlock$1("tr", {
                            key: itemPath(item)
                          }, [
                            _createElementVNode$1("td", null, [
                              _createVNode$1(_component_VCheckbox, {
                                "model-value": isSelected(item),
                                density: "compact",
                                "hide-details": "",
                                "onUpdate:modelValue": $event => (toggleEntry(item))
                              }, null, 8, ["model-value", "onUpdate:modelValue"])
                            ]),
                            _createElementVNode$1("td", null, [
                              _createElementVNode$1("div", {
                                class: "aisub-entry-name",
                                onClick: $event => (item.type === 'dir' ? enterEntry(item) : toggleEntry(item))
                              }, [
                                _createVNode$1(_component_VIcon, {
                                  size: "18",
                                  icon: item.type === 'dir' ? 'mdi-folder' : 'mdi-file-video-outline',
                                  color: item.type === 'dir' ? 'warning' : 'primary'
                                }, null, 8, ["icon", "color"]),
                                _createElementVNode$1("span", _hoisted_6, _toDisplayString$1(item.name), 1),
                                (item.type === 'dir')
                                  ? (_openBlock$1(), _createBlock$1(_component_VBtn, {
                                      key: 0,
                                      size: "x-small",
                                      variant: "text",
                                      icon: "mdi-arrow-right",
                                      onClick: _withModifiers($event => (enterEntry(item)), ["stop"])
                                    }, null, 8, ["onClick"]))
                                  : _createCommentVNode$1("", true)
                              ], 8, _hoisted_5)
                            ]),
                            _createElementVNode$1("td", _hoisted_7, _toDisplayString$1(item.type === 'dir' ? '-' : _unref(formatSize)(item.size)), 1),
                            _createElementVNode$1("td", _hoisted_8, _toDisplayString$1(_unref(formatTime)(item.modify_time)), 1)
                          ]))
                        }), 128))
                      ])
                    ]),
                    _: 1
                  }))
            ]),
            _: 1
          }),
          _createVNode$1(_component_VDivider),
          _createVNode$1(_component_VCardActions, { class: "aisub-picker-actions" }, {
            default: _withCtx$1(() => [
              _createElementVNode$1("span", _hoisted_9, "已选 " + _toDisplayString$1(selectedCount.value) + " 项", 1),
              _createVNode$1(_component_VBtn, {
                variant: "text",
                size: "small",
                onClick: selectVisible
              }, {
                default: _withCtx$1(() => [...(_cache[10] || (_cache[10] = [
                  _createTextVNode$1("全选本页", -1)
                ]))]),
                _: 1
              }),
              _createVNode$1(_component_VBtn, {
                variant: "text",
                size: "small",
                disabled: !selectedCount.value,
                onClick: clearSelection
              }, {
                default: _withCtx$1(() => [...(_cache[11] || (_cache[11] = [
                  _createTextVNode$1("清空", -1)
                ]))]),
                _: 1
              }, 8, ["disabled"]),
              _createVNode$1(_component_VSpacer),
              _createVNode$1(_component_VBtn, {
                variant: "text",
                "prepend-icon": "mdi-folder-plus-outline",
                onClick: addCurrentFolder
              }, {
                default: _withCtx$1(() => [...(_cache[12] || (_cache[12] = [
                  _createTextVNode$1(" 添加当前文件夹 ", -1)
                ]))]),
                _: 1
              }),
              _createVNode$1(_component_VBtn, {
                variant: "text",
                onClick: close
              }, {
                default: _withCtx$1(() => [...(_cache[13] || (_cache[13] = [
                  _createTextVNode$1("取消", -1)
                ]))]),
                _: 1
              }),
              _createVNode$1(_component_VBtn, {
                color: "primary",
                "prepend-icon": "mdi-check",
                disabled: !selectedCount.value,
                onClick: confirmSelection
              }, {
                default: _withCtx$1(() => [...(_cache[14] || (_cache[14] = [
                  _createTextVNode$1(" 添加所选 ", -1)
                ]))]),
                _: 1
              }, 8, ["disabled"])
            ]),
            _: 1
          })
        ]),
        _: 1
      })
    ]),
    _: 1
  }, 8, ["model-value", "fullscreen"]))
}
}

};
const FilePickerDialog = /*#__PURE__*/_export_sfc(_sfc_main$1, [['__scopeId',"data-v-2a2d0b7f"]]);

const {createElementVNode:_createElementVNode,resolveComponent:_resolveComponent,createVNode:_createVNode,createTextVNode:_createTextVNode,withCtx:_withCtx,toDisplayString:_toDisplayString,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "aisub-config" };
const _hoisted_2 = { class: "aisub-config-shell" };
const _hoisted_3 = { class: "aisub-config-footer" };

const {computed,onMounted,reactive,ref,watch} = await importShared('vue');

/**
 * AiSubV2 配置页：复用后端 get_form() 的 Vuetify JSON 表单，
 * 并为媒体路径补充“从文件管理器选择”弹窗。
 */

const _sfc_main = {
  __name: 'Config',
  props: {
  api: { type: Object, default: () => ({}) },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'AiSubV2' },
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
const saving = ref(false);
const pickerOpen = ref(false);
const pickerKey = ref('path_list');

// 选择弹窗的起始目录：优先使用已填写的第一个路径
const pickerInitialPath = computed(() => {
  const key = pickerKey.value || 'path_list';
  const first = splitPathLines(config[key])[0] || '';
  if (!first) return ''
  const name = baseName(first);
  return isVideoFile(name) ? parentPath(first) : first
});

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
    applyConfig({ ...(body.model || {}), ...(props.initialConfig || {}) });
    if (!schema.length) loadError.value = '配置表单加载为空，请点击重试';
  } catch (error) {
    loadError.value = errorMessage(error, '配置加载失败，请点击重试');
  } finally {
    loading.value = false;
  }
}

function save() {
  saving.value = true;
  try {
    emit('save', { ...config });
  } finally {
    saving.value = false;
  }
}

function openPicker(modelKey) {
  pickerKey.value = typeof modelKey === 'string' && modelKey ? modelKey : 'path_list';
  pickerOpen.value = true;
}

// 将弹窗中选择的路径追加到目标配置项（自动去重）
function appendPickedPaths(paths) {
  const key = pickerKey.value || 'path_list';
  const merged = splitPathLines(config[key]);
  for (const path of paths || []) {
    const value = String(path || '').trim();
    if (value && !merged.includes(value)) merged.push(value);
  }
  config[key] = merged.join('\n');
}

watch(() => props.initialConfig, value => {
  if (value && Object.keys(value).length) applyConfig({ ...config, ...value });
}, { deep: true });

onMounted(loadForm);

return (_ctx, _cache) => {
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
        _cache[7] || (_cache[7] = _createElementVNode("div", { class: "text-h6 ms-3" }, "AI字幕生成配置", -1)),
        _createVNode(_component_VSpacer),
        _createVNode(_component_VBtn, {
          variant: "text",
          "prepend-icon": "mdi-format-list-bulleted",
          onClick: _cache[0] || (_cache[0] = $event => (emit('switch')))
        }, {
          default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
            _createTextVNode("查看任务", -1)
          ]))]),
          _: 1
        }),
        _createVNode(_component_VBtn, {
          color: "primary",
          variant: "tonal",
          "prepend-icon": "mdi-content-save",
          loading: saving.value,
          onClick: save
        }, {
          default: _withCtx(() => [...(_cache[6] || (_cache[6] = [
            _createTextVNode(" 保存 ", -1)
          ]))]),
          _: 1
        }, 8, ["loading"]),
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
                default: _withCtx(() => [...(_cache[8] || (_cache[8] = [
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
            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(conf.value, (node, index) => {
              return (_openBlock(), _createBlock(JsonFormNode, {
                key: index,
                config: node,
                model: config,
                "on-pick": openPicker
              }, null, 8, ["config", "model"]))
            }), 128)),
            _createElementVNode("div", _hoisted_3, [
              _createVNode(_component_VBtn, {
                variant: "text",
                "prepend-icon": "mdi-format-list-bulleted",
                onClick: _cache[2] || (_cache[2] = $event => (emit('switch')))
              }, {
                default: _withCtx(() => [...(_cache[9] || (_cache[9] = [
                  _createTextVNode("查看任务", -1)
                ]))]),
                _: 1
              }),
              _createVNode(_component_VSpacer),
              _createVNode(_component_VBtn, {
                variant: "text",
                onClick: _cache[3] || (_cache[3] = $event => (emit('close')))
              }, {
                default: _withCtx(() => [...(_cache[10] || (_cache[10] = [
                  _createTextVNode("关闭", -1)
                ]))]),
                _: 1
              }),
              _createVNode(_component_VBtn, {
                color: "primary",
                "prepend-icon": "mdi-content-save",
                loading: saving.value,
                onClick: save
              }, {
                default: _withCtx(() => [...(_cache[11] || (_cache[11] = [
                  _createTextVNode("保存", -1)
                ]))]),
                _: 1
              }, 8, ["loading"])
            ])
          ], 64))
    ]),
    _createVNode(FilePickerDialog, {
      modelValue: pickerOpen.value,
      "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((pickerOpen).value = $event)),
      api: __props.api,
      "initial-path": pickerInitialPath.value,
      onConfirm: appendPickedPaths
    }, null, 8, ["modelValue", "api", "initial-path"])
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-d8ca43db"]]);

export { Config as default };
