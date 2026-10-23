// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { h, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { LmInput } from '../../src'
import { mountWithProvider } from '../helpers/mount'

describe('LmInput 透传壳', () => {
  it('渲染原生 input 且透传 attrs(placeholder)', () => {
    const wrapper = mount(LmInput, { props: { placeholder: '请输入名称' } })
    const input = wrapper.find('input')
    expect(input.exists()).toBe(true)
    expect(input.attributes('placeholder')).toBe('请输入名称')
  })

  it('value/onUpdate:value 协议:输入回传新值,外部变更回显', async () => {
    // naive-ui 系组件走 value/onUpdate:value 协议(项目约定,见 hooks form/render.ts);
    // LmInput 为透传壳,监听器经 $attrs 直达 n-input,不经过自身 emitter,故用回调断言
    const updated: string[] = []
    const wrapper = mount(LmInput, {
      props: { value: '', 'onUpdate:value': (value: string) => updated.push(value) },
    })
    await wrapper.find('input').setValue('hello')
    expect(updated).toEqual(['hello'])

    await wrapper.setProps({ value: 'world' })
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('world')
  })

  it('透传插槽(prefix)与禁用态', () => {
    const wrapper = mount(LmInput, {
      props: { disabled: true },
      slots: { prefix: () => h('span', { class: 'prefix-mark' }, '¥') },
    })
    expect(wrapper.find('.prefix-mark').text()).toBe('¥')
    expect((wrapper.find('input').element as HTMLInputElement).disabled).toBe(true)
  })

  it('mountWithProvider helper 链路可用(NaiveProvider 内正常渲染)', () => {
    const wrapper = mountWithProvider(() => h(LmInput, { placeholder: 'via-provider' }))
    expect(wrapper.find('input').attributes('placeholder')).toBe('via-provider')
  })

  it('宿主组件外层回调可接收内层组件更新(v-model 场景铺垫)', async () => {
    const seen: string[] = []
    const wrapper = mountWithProvider(() =>
      h(LmInput, {
        value: 'init',
        'onUpdate:value': (value: string) => seen.push(value),
      }),
    )
    await wrapper.find('input').setValue('typed')
    expect(seen).toEqual(['typed'])
  })

  it('ref 在 render 函数闭包内响应式生效', async () => {
    const text = ref('a')
    const wrapper = mountWithProvider(() => h(LmInput, { value: text.value }))
    text.value = 'b'
    await wrapper.vm.$nextTick()
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('b')
  })
})
