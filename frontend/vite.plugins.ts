import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'
import type { ComponentResolver } from 'unplugin-vue-components/types'
import type { PluginOption } from 'vite'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

interface AppPluginOptions {
  /** 是否自动引入 vue-router 的 API(useRouter、useRoute 等) */
  router?: boolean
  /** 是否自动引入 pinia 的 API(defineStore、storeToRefs 等) */
  pinia?: boolean
}

/**
 * 解析 Lm 前缀组件到 @repo/naive-ui 具名导出。
 * 模板中 <LmForm>、<LmInput> 等无需显式 import,与 NaiveUiResolver 同构。
 */
function LmResolver(): ComponentResolver {
  return {
    type: 'component',
    resolve: (name: string) => {
      if (name.startsWith('Lm')) {
        return { name, from: '@repo/naive-ui' }
      }
    },
  }
}

/**
 * 为业务应用创建统一的「按需自动引入」插件集合:
 * - AutoImport:自动引入 vue / vue-router / pinia 的 Composition API 与工具函数,模板表达式同样生效。
 * - Components:配合 NaiveUiResolver 自动引入 naive-ui 官方组件(模板中的 <n-button> 等);
 *   配合 LmResolver 自动引入 @repo/naive-ui 的 Lm 前缀封装组件(模板中的 <LmForm> 等)。
 *
 * 两个插件的声明文件生成到各应用 cwd 下的 src/auto-imports.d.ts 与 src/components.d.ts,
 * 已被各应用 tsconfig 的 src 目录匹配规则覆盖,无需额外调整 include。
 *
 * 注意:@repo/naive-ui 的非组件导出(NaiveProvider、createDiscreteApi、useTheme 等)
 * 仍不在自动引入范围,需在源码中显式 import。
 */
export function createAutoImportPlugins(options: AppPluginOptions = {}): PluginOption[] {
  const { router = false, pinia = false } = options

  const imports: Array<string> = ['vue']
  if (router) imports.push('vue-router')
  if (pinia) imports.push('pinia')

  return [
    AutoImport({
      imports,
      vueTemplate: true,
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [NaiveUiResolver(), LmResolver()],
      dts: 'src/components.d.ts',
    }),
  ]
}

/**
 * 将根目录 theme.boot.js 内联注入 index.html <head> 最前面,
 * 使主题(明暗 + 主色)在业务 bundle 加载前同步就位,避免首屏闪屏。
 * 三个应用共用同一份脚本,存储键约定见脚本头部注释。
 */
export function injectThemeBootPlugin(): PluginOption {
  const bootScript = readFileSync(fileURLToPath(new URL('./theme.boot.js', import.meta.url)), 'utf-8')
  return {
    name: 'inject-theme-boot',
    transformIndexHtml: () => [
      { tag: 'script', children: bootScript, injectTo: 'head-prepend' },
    ],
  }
}
