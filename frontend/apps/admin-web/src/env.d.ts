/// <reference types="vite/client" />

// export {} 使本文件成为 ES module,其内的 declare module 'vue-router' 作为增强(augmentation)
// 而非全新模块声明,避免覆盖真实 vue-router 的类型导出。
export {}

declare module '*.scss'
declare module 'virtual:uno.css'
declare module '@repo/naive-ui/styles'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    /** 全局搜索归属页面:已合并为某页面子 tab 的 redirect 路由,声明其操作项/归属小字归该页面(name) */
    searchGroup?: string
  }
}
