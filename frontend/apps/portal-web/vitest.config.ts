import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import { defineConfig } from 'vitest/config'

// 业务单测:逻辑层 mock 隔离,组件挂载用 happy-dom + plugin-vue。
// @repo/* alias 对齐 apps/*/vite.config.ts:包源码内反向引用按 app 端约定解析到源码。
export default defineConfig({
  plugins: [vue(), AutoImport({ imports: ['vue', 'vue-router'], dts: false })],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@repo/hooks': fileURLToPath(new URL('../../packages/hooks/src/index.ts', import.meta.url)),
      '@repo/http-client': fileURLToPath(
        new URL('../../packages/http-client/src/index.ts', import.meta.url),
      ),
      '@repo/naive-ui': fileURLToPath(new URL('../../packages/naive-ui/src', import.meta.url)),
    },
  },
  test: {
    include: ['tests/**/*.test.ts'],
  },
})
