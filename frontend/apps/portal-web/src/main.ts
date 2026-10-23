import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import 'virtual:uno.css'
import '@repo/naive-ui/styles'

// 等初始导航完成(懒加载路由 chunk)再挂载,避免先挂载空 RouterView 造成白屏间隙;
// 初始导航失败也照常挂载,保持与旧版一致的可渲染兜底
const app = createApp(App).use(router)
router.isReady().catch(() => undefined).then(() => app.mount('#app'))
