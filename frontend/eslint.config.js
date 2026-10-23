import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'

export default [
  { ignores: ['**/dist/**', '**/node_modules/**', '**/.turbo/**', '**/auto-imports.d.ts', '**/components.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
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
