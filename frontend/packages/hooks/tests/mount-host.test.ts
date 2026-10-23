// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { ComponentPublicInstance } from 'vue'
import { closeAllDialog, useDialog } from '../src'
import { mountWithHost } from './helpers/mount'

describe('mountWithHost 挂载基础设施', () => {
  let wrapper: VueWrapper<ComponentPublicInstance> | null = null

  afterEach(() => {
    closeAllDialog()
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('useDialog 命令式弹窗在 DialogHost 内渲染可见', async () => {
    wrapper = mountWithHost(() => h('div', { class: 'app-root' }, 'app'))
    useDialog({
      title: '基础设施验证',
      content: () => h('div', { class: 'host-dialog-content' }, '弹窗内容'),
    })
    // 队列 push 触发 DialogHost 异步重渲染,等一拍再断言
    await nextTick()
    // NModal teleport 到 body,断言需查 document 而非 wrapper 根
    expect(document.body.textContent).toContain('弹窗内容')
    expect(document.body.textContent).toContain('基础设施验证')
  })

  it('close 移除弹窗,closeAllDialog 清场', async () => {
    wrapper = mountWithHost(() => h('div'))
    const handle = useDialog({ title: 't', content: () => h('div', 'x') })
    await nextTick()
    expect(document.body.textContent).toContain('x')

    handle.close()
    await nextTick()
    expect(document.body.textContent).not.toContain('x')

    useDialog({ title: 't2', content: () => h('div', 'y') })
    await nextTick()
    closeAllDialog()
    await nextTick()
    expect(document.body.textContent).not.toContain('y')
  })
})
