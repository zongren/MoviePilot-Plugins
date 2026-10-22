import { createApp } from 'vue'
import Config from './components/Config.vue'

// 仅用于本地开发预览，运行时不使用该入口。
createApp(Config, { initialConfig: {}, api: window.MoviePilotAPI || {} }).mount('#app')
