// @vitest-environment happy-dom
import { defineComponent, h, type VNodeChild } from 'vue'
import { mount } from '@vue/test-utils'
import { NaiveProvider } from '@repo/naive-ui'
import { DialogHost, MessageHost } from '../../src'

/**
 * 在完整宿主环境(NaiveProvider + DialogHost + MessageHost)内挂载内容,
 * 供 useDialog/useConfirm/useMessage 及弹窗内容组件的用例复用:
 * 命令式弹窗/消息队列由模块级 dialogQueue/messageQueue 驱动,必须挂 Host 才会渲染。
 *
 * 默认 attachTo document.body:NModal/NMessage 均 teleport 到 body,
 * 游离挂载(默认行为)下 teleport 不可见。调用方负责在 afterEach unmount 返回的 wrapper。
 * 用例文件需注解 `@vitest-environment happy-dom`。
 */
export function mountWithHost(
  render: () => VNodeChild,
  options: Omit<Parameters<typeof mount>[1], 'attachTo'> = {},
) {
  return mount(
    defineComponent({
      setup: () => () =>
        h(NaiveProvider, () => [
          h(DialogHost, { key: 'dialog-host' }),
          h(MessageHost, { key: 'message-host' }),
          render(),
        ]),
    }),
    { ...options, attachTo: document.body },
  )
}
