import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueJsx from '@vitejs/plugin-vue-jsx'
import UnoCSS from 'unocss/vite'
import { fileURLToPath, URL } from 'node:url'
import { createAutoImportPlugins, injectThemeBootPlugin } from '../../vite.plugins.ts'

// 控制平面后端（编排 WebSocket + BFF REST）
// 地址来自根目录 env 的 API_TARGET(.env.local;--mode test 时读 .env.test)

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('../..', import.meta.url)), '')
  // API_TARGET 仅被 dev server proxy 消费,build 不依赖;缺失时只阻断 dev,避免干净环境 pnpm build 失败
  if (command === 'serve' && !env.API_TARGET) {
    throw new Error(
      '[vite.config] 缺少 API_TARGET:请在仓库根目录 .env.local 配置控制平面后端地址(参考 .env.example)',
    )
  }
  const API_TARGET = env.API_TARGET || 'http://127.0.0.1:8080'
  return {
    base: '/admin/',
    plugins: [
      vue(),
      vueJsx(),
      UnoCSS({ configFile: fileURLToPath(new URL('../../uno.config.ts', import.meta.url)) }),
      injectThemeBootPlugin(),
      ...createAutoImportPlugins({ router: true, pinia: true }),
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
        '@repo/hooks': fileURLToPath(new URL('../../packages/hooks/src/index.ts', import.meta.url)),
        '@repo/http-client': fileURLToPath(
          new URL('../../packages/http-client/src/index.ts', import.meta.url),
        ),
        '@repo/naive-ui/styles': fileURLToPath(
          new URL('../../packages/naive-ui/src/styles/index.scss', import.meta.url),
        ),
        '@repo/naive-ui': fileURLToPath(new URL('../../packages/naive-ui/src', import.meta.url)),
        '@repo/ui-automator': fileURLToPath(
          new URL('../../packages/ui-automator/src/index.ts', import.meta.url),
        ),
      },
    },
    build: {
      // gzip 全量体积计算拖慢构建,上传/体积分析时按需临时开启
      reportCompressedSize: false,
    },
    server: {
      port: 5174,
      // 端口被占时直接报错退出，避免静默递增端口导致 portal proxy 目标错位
      strictPort: true,
      // 直连 5174 开发时,业务接口原样转发到控制平面(baseURL 已含 /api/v1 前缀)
      proxy: {
        '/api': { target: API_TARGET, changeOrigin: true },
      },
    },
  }
})
