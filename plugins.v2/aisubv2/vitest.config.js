import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端组件测试（jsdom + Vuetify），与联邦构建配置相互独立。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.spec.js'],
    setupFiles: ['tests/frontend/setup.js'],
    globals: true,
    // Vuetify 组件源码带有 CSS 副作用导入，交给 Vite 处理而不是 Node ESM 加载器
    server: {
      deps: {
        inline: ['vuetify'],
      },
    },
  },
})
