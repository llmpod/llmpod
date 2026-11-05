import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'

export default [
  { ignores: ['**/dist/**', '**/node_modules/**', '**/.turbo/**', '**/auto-imports.d.ts', '**/components.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // 只取 essential(正确性):recommended 叠加的 max-attributes-per-line、
  // singleline-html-element-content-newline、html-indent 等属于格式化规则,
  // 与本仓 Prettier(.prettierrc: printWidth 100 / 单引号 / 无分号)职责重叠且互相打架,
  // 会稳定产出数百条无法收敛的 warning。格式统一交给 `pnpm format`。
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        // 支持 <script setup lang="tsx"> 中的 JSX 语法
        ecmaFeatures: { jsx: true },
      },
    },
  },
  {
    rules: {
      'vue/multi-word-component-names': 'off',
      // TS 项目已由 vue-tsc/typecheck 覆盖未定义检查;且 unplugin-auto-import 引入的
      // 隐式全局(如 ref、useRouter)会被 no-undef 误报,故关闭该规则。
      'no-undef': 'off',
    },
  },
]
