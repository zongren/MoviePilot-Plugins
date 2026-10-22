<script setup>
import { computed } from 'vue'

/**
 * 渲染后端 get_form() 返回的 Vuetify JSON 节点。
 * 与宿主 FormRender 行为保持一致：支持 model / v-show / v-model:xxx / {{表达式}} / onXxx 事件。
 */
const props = defineProps({
  config: { type: Object, required: true },
  model: { type: Object, required: true },
})

const node = computed(() => props.config || {})
const children = computed(() => (Array.isArray(node.value.content) ? node.value.content : []))
const hasText = computed(() => node.value.text !== undefined && node.value.text !== null)
const componentName = computed(() => node.value.component || 'div')

// 以 model 为作用域求值 {{...}} 表达式或 v-show 条件
function evaluate(expression) {
  try {
    // eslint-disable-next-line no-new-func
    return new Function('model', `with(model){ return (${expression}) }`)(props.model)
  } catch (error) {
    console.error('JSON 表单表达式执行失败:', expression, error)
    return undefined
  }
}

function buildProps(rawProps) {
  const result = {}
  const isExpression = value => typeof value === 'string'
    && value.startsWith('{{') && value.endsWith('}}')
  for (const [key, value] of Object.entries(rawProps || {})) {
    if (key.toLowerCase() === 'modelvalue') {
      result.value = props.model[value]
      result['onUpdate:value'] = next => { props.model[value] = next }
    } else if (key === 'model' || key === 'v-model') {
      result.modelValue = props.model[value]
      result['onUpdate:modelValue'] = next => { props.model[value] = next }
    } else if (key === 'show' || key === 'v-show') {
      const visible = evaluate(value)
      result.style = { ...(result.style || {}), display: visible ? '' : 'none' }
    } else if (key.startsWith('model:')) {
      const target = key.slice('model:'.length)
      result[target] = props.model[value]
      result[`onUpdate:${target}`] = next => { props.model[value] = next }
    } else if (key.startsWith('on') && typeof value === 'string') {
      result[key] = event => {
        try {
          // eslint-disable-next-line no-new-func
          return new Function('model', 'event', `with(model){ return (${value})(event) }`)(
            props.model, event,
          )
        } catch (error) {
          console.error('JSON 表单事件执行失败:', key, error)
        }
      }
    } else if (isExpression(value)) {
      result[key] = evaluate(value.slice(2, -2).trim())
    } else if (typeof value === 'string' && Object.prototype.hasOwnProperty.call(props.model, value)) {
      result[key] = props.model[value]
    } else {
      result[key] = value
    }
  }
  return result
}

const boundProps = computed(() => buildProps(node.value.props))
const textContent = computed(() => (
  typeof node.value.text === 'string' ? node.value.text : props.model[node.value.text]
))
</script>

<template>
  <component :is="componentName" v-bind="boundProps">
    <template v-if="hasText">{{ textContent }}</template>
    <template v-else-if="children.length">
      <JsonFormNode
        v-for="(child, index) in children"
        :key="index"
        :config="child"
        :model="model"
      />
    </template>
  </component>
</template>
