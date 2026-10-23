import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// hooks 单测:纯逻辑用例默认 node 环境按需注解 jsdom;
// 挂载 helper(tests/helpers)引 @repo/naive-ui 的 .vue 组件,需 plugin-vue 编译。
// @repo/* alias 对齐 apps/*/vite.config.ts:naive-ui 源码(如 LmLayout)反向引用
// @repo/hooks,自引用在测试环境需显式映射到源码。
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ['tests/**/*.test.ts'],
  },
  resolve: {
    alias: {
      '@repo/hooks': fileURLToPath(new URL('./src/index.ts', import.meta.url)),
      '@repo/naive-ui': fileURLToPath(new URL('../naive-ui/src/index.ts', import.meta.url)),
      '@repo/http-client': fileURLToPath(new URL('../http-client/src/index.ts', import.meta.url)),
    },
  },
})
