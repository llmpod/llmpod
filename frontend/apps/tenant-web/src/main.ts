import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import 'virtual:uno.css'
import '@repo/naive-ui/styles'
import './styles/index.scss'

// 等初始导航完成(守卫拉 profile + 懒加载路由 chunk)再挂载,挂载即替换 #app 内的 splash,
// 首屏内容就绪才移除 splash;若提前 mount 会先用空 RouterView 顶掉 splash 造成白屏间隙。
// 初始导航失败(接口 500/断网等)也照常挂载:MessageHost 随 App 渲染,保持错误提示可见,避免卡 splash
const app = createApp(App).use(createPinia()).use(router)
router.isReady().catch(() => undefined).then(() => app.mount('#app'))
