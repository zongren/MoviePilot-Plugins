// 插件前端公共工具：接口解包、错误信息提取与展示格式化。

// MoviePilot 的 axios 实例已经剥离了一层 response，这里兼容两种返回结构
export function unwrapResponse(response) {
  if (!response) return response
  return response?.data?.data ?? response?.data ?? response
}

// 统一提取接口错误信息
export function errorMessage(error, fallback = '操作失败') {
  if (!error) return fallback
  return error?.response?.data?.detail
    || error?.response?.data?.message
    || error?.data?.detail
    || error?.data?.message
    || error?.message
    || fallback
}

// 安全地把任意值转成数组，接口缺字段时返回空数组
export function asArray(value) {
  return Array.isArray(value) ? value : []
}

// 安全地把任意值转成对象，接口缺字段时返回空对象
export function asObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

// 安全地转成数字，非法值（null/undefined/NaN）使用兜底值
export function asNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

// 把字节数格式化为易读文本
export function formatSize(bytes) {
  const size = asNumber(bytes, 0)
  if (size <= 0) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

// 把秒级 Unix 时间戳格式化为本地时间文本
export function formatTime(timestamp) {
  const value = asNumber(timestamp, 0)
  if (value <= 0) return '-'
  const date = new Date(value * 1000)
  const pad = number => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

// 把秒数格式化为“x 秒 / x 分 y 秒 / x 小时 y 分”
export function formatDuration(seconds) {
  const value = asNumber(seconds, 0)
  if (value <= 0) return '-'
  if (value < 60) return `${value.toFixed(value >= 10 ? 0 : 1)} 秒`
  const minutes = Math.floor(value / 60)
  const rest = Math.round(value % 60)
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  return restMinutes ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`
}

// 把“到期时间戳 - 当前时间戳”格式化为剩余时间文本
export function formatRemaining(dueAt, now, emptyText = '-') {
  const due = asNumber(dueAt, 0)
  if (due <= 0) return emptyText
  const current = asNumber(now, Math.floor(Date.now() / 1000))
  const remaining = due - current
  if (remaining <= 0) return '已到期'
  if (remaining < 60) return `${Math.ceil(remaining)} 秒`
  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

// 千分位数字展示，0 也正常展示
export function formatCount(value) {
  const number = asNumber(value, 0)
  return number.toLocaleString('zh-CN')
}
