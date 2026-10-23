import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 组件库单测:.vue 组件挂载用例按文件注解 @vitest-environment happy-dom,
// 纯逻辑用例默认 node 环境,保持与 hooks/http-client 相同的按需注解约定。
// @repo/* alias 对齐 apps/*/vite.config.ts:包源码内 @repo/hooks 等引用
// 依赖消费方 app 的 alias 解析,测试环境同样映射到源码。
export default defineConfig({
  plugins: [vue()],
  test: {
    include: ['tests/**/*.test.ts'],
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@repo/naive-ui': fileURLToPath(new URL('./src', import.meta.url)),
      '@repo/hooks': fileURLToPath(new URL('../hooks/src/index.ts', import.meta.url)),
      '@repo/http-client': fileURLToPath(new URL('../http-client/src/index.ts', import.meta.url)),
    },
  },
})
