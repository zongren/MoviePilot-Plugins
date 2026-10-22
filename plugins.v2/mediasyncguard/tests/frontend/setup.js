import { vi } from 'vitest'

// jsdom 缺少 Vuetify 组件依赖的浏览器 API，这里补齐最小实现。

if (!window.matchMedia) {
  window.matchMedia = query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })
}

class NoopObserver {
  observe() {}

  unobserve() {}

  disconnect() {}

  takeRecords() {
    return []
  }
}

globalThis.ResizeObserver = globalThis.ResizeObserver || NoopObserver
globalThis.IntersectionObserver = globalThis.IntersectionObserver || NoopObserver

if (!window.scrollTo) {
  window.scrollTo = () => {}
}

if (!window.visualViewport) {
  Object.defineProperty(window, 'visualViewport', {
    configurable: true,
    value: {
      width: 1024,
      height: 768,
      scale: 1,
      offsetLeft: 0,
      offsetTop: 0,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    },
  })
}
