// 插件前端公共工具：接口解包、路径处理与视频后缀判断。

// 常见视频文件后缀（与 MoviePilot 媒体库后缀保持一致的使用习惯）
export const VIDEO_EXTENSIONS = [
  'mp4', 'mkv', 'ts', 'm2ts', 'mts', 'iso', 'rmvb', 'rm', 'avi', 'mov',
  'wmv', 'flv', 'webm', 'mpg', 'mpeg', 'm4v', '3gp', 'divx', 'vob', 'f4v',
  'asf', 'ogm', 'ogv', 'tp', 'trp',
]

// MoviePilot 的 axios 实例已经剥离了一层 response，这里兼容两种返回结构
export function unwrapResponse(response) {
  if (!response) return response
  return response?.data?.data ?? response?.data ?? response
}

// 统一提取接口错误信息
export function errorMessage(error, fallback = '操作失败') {
  if (!error) return fallback
  return error?.response?.data?.detail || error?.response?.data?.message || error?.message || fallback
}

// 判断文件名是否是视频文件
export function isVideoFile(name) {
  const text = String(name || '')
  const index = text.lastIndexOf('.')
  if (index < 0) return false
  return VIDEO_EXTENSIONS.includes(text.slice(index + 1).toLowerCase())
}

// 把多行文本解析为去空行的路径列表
export function splitPathLines(text) {
  return String(text ?? '')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
}

// 拼接目录与文件名，避免出现重复的斜杠
export function joinPath(dir, name) {
  const base = String(dir || '/').replace(/\/+$/, '')
  const leaf = String(name || '').replace(/^\/+/, '')
  if (!leaf) return base || '/'
  return `${base}/${leaf}`
}

// 取父目录，根目录的父目录仍是根目录
export function parentPath(path) {
  const text = String(path || '/').replace(/\/+$/, '')
  if (!text || text === '') return '/'
  const index = text.lastIndexOf('/')
  if (index <= 0) return '/'
  return text.slice(0, index)
}

// 取路径最后一段作为名称
export function baseName(path) {
  const text = String(path || '').replace(/\/+$/, '')
  const index = text.lastIndexOf('/')
  return index < 0 ? text : text.slice(index + 1)
}

// 把字节数格式化为易读文本
export function formatSize(bytes) {
  const size = Number(bytes)
  if (!size || size <= 0) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

// 把秒级时间戳格式化为本地时间文本
export function formatTime(timestamp) {
  const value = Number(timestamp)
  if (!value || value <= 0) return '-'
  const date = new Date(value * 1000)
  const pad = number => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}
