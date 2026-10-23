import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import vueJsx from '@vitejs/plugin-vue-jsx'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'
import type { ComponentResolver } from 'unplugin-vue-components/types'
import { defineConfig } from 'vitest/config'

/** 对齐 vite.plugins.ts 的 LmResolver:<Lm*> 解析到 @repo/naive-ui 具名导出 */
function LmResolver(): ComponentResolver {
  return {
    type: 'component',
    resolve: (name: string) => (name.startsWith('Lm') ? { name, from: '@repo/naive-ui' } : undefined),
  }
}

// 业务单测:仅测 ts/tsx 逻辑层(useStore/useController),.vue 组件与 api 层一律 mock。
// AutoImport 对齐 vite.plugins.ts:controller 依赖自动注入的 ref/computed/onMounted 等。
// plugin-vue:挂载 LmForm 等 naive-ui 封装组件时需编译 .vue;vueJsx:.tsx 内 JSX(如 brand/config controller)。
// Components:挂载模板含 <n-*>/<Lm*> 的 .vue 步骤组件(M4 组件层)。
// @repo/* alias 对齐 vite.config.ts:包源码内反向引用(如 LmLayout → @repo/hooks)按 app 端约定解析到源码。
export default defineConfig({
  plugins: [
    vue(),
    vueJsx(),
    AutoImport({ imports: ['vue', 'vue-router', 'pinia'], dts: false }),
    Components({ resolvers: [NaiveUiResolver(), LmResolver()], dts: false }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@repo/hooks': fileURLToPath(new URL('../../packages/hooks/src/index.ts', import.meta.url)),
      '@repo/http-client': fileURLToPath(
        new URL('../../packages/http-client/src/index.ts', import.meta.url),
      ),
      '@repo/naive-ui': fileURLToPath(new URL('../../packages/naive-ui/src', import.meta.url)),
      '@repo/ui-automator': fileURLToPath(
        new URL('../../packages/ui-automator/src/index.ts', import.meta.url),
      ),
    },
  },
  test: {
    include: ['tests/**/*.test.ts'],
  },
})
