// @vitest-environment happy-dom
import { defineComponent, h, type VNodeChild } from 'vue'
import { mount } from '@vue/test-utils'
import { NaiveProvider } from '../../src'

/**
 * 在 NaiveProvider(naive 全家桶 provider:config/message/dialog/notification/loading-bar)内挂载内容。
 * 传渲染函数而非组件:props/v-model/事件绑定都在调用方闭包内完成,helper 不掺和属性透传。
 * 用例文件需注解 `@vitest-environment happy-dom`。
 */
export function mountWithProvider(render: () => VNodeChild, options: Parameters<typeof mount>[1] = {}) {
  return mount(
    defineComponent({
      setup: () => () => h(NaiveProvider, () => render()),
    }),
    options,
  )
}
