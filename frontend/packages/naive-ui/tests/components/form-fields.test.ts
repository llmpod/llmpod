// @vitest-environment happy-dom
import { h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { NSelect } from 'naive-ui'
import {
  LmCheckboxGroup,
  LmDatePicker,
  LmInput,
  LmInputNumber,
  LmPassword,
  LmRadioGroup,
  LmSelect,
  LmSwitch,
  LmTextarea,
  LmTreeSelect,
} from '../../src'
import { mountWithProvider } from '../helpers/mount'

describe('文本类字段', () => {
  it('LmTextarea 渲染 textarea,LmPassword 渲染 password 输入', () => {
    const textarea = mount(LmTextarea)
    expect(textarea.find('textarea').exists()).toBe(true)

    const password = mount(LmPassword)
    expect(password.find('input[type=password]').exists()).toBe(true)
  })

  it('LmInputNumber 默认隐藏步进按钮(showButton:false)', () => {
    const wrapper = mount(LmInputNumber)
    expect(wrapper.find('.n-input-number button, .n-input-number .n-input-number-suffix button').exists()).toBe(false)

    const withButton = mount(LmInputNumber, { props: { showButton: true } })
    // showButton 渲染递增/递减按钮
    expect(withButton.findAll('button').length).toBeGreaterThan(0)
  })

  it('LmInputNumber value/onUpdate:value 双向协议', async () => {
    const updated: Array<number | null> = []
    const wrapper = mount(LmInputNumber, {
      props: { value: 1, 'onUpdate:value': (value: number | null) => updated.push(value) },
    })
    await wrapper.find('input').setValue('42')
    expect(updated).toContain(42)

    await wrapper.setProps({ value: 7 })
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('7')
  })
})

describe('选择类字段', () => {
  it('LmSwitch 点击切换并回传布尔值', async () => {
    const updated: boolean[] = []
    const wrapper = mount(LmSwitch, {
      props: { value: false, 'onUpdate:value': (value: boolean) => updated.push(value) },
    })
    await wrapper.find('.n-switch').trigger('click')
    expect(updated).toEqual([true])
  })

  it('LmRadioGroup 按 options 渲染选项,button 模式渲染按钮组', () => {
    const options = [
      { label: 'vLLM', value: 'vllm' },
      { label: 'SGLang', value: 'sglang', disabled: true },
    ]
    const wrapper = mount(LmRadioGroup, { props: { options } })
    expect(wrapper.text()).toContain('vLLM')
    expect(wrapper.text()).toContain('SGLang')

    const buttons = mount(LmRadioGroup, { props: { options, button: true } })
    expect(buttons.find('.n-radio-button').exists()).toBe(true)
  })

  it('LmCheckboxGroup 按 options 渲染,勾选回传数组', async () => {
    const updated: string[][] = []
    const wrapper = mount(LmCheckboxGroup, {
      props: {
        options: [
          { label: 'A', value: 'a' },
          { label: 'B', value: 'b' },
        ],
        value: [],
        'onUpdate:value': (value: string[]) => updated.push(value),
      },
    })
    expect(wrapper.text()).toContain('A')
    await wrapper.findAll('.n-checkbox')[0].trigger('click')
    expect(updated.at(-1)).toContain('a')
  })

  it('LmSelect 空值归一:value="" 视为未选归一为 null', () => {
    // 业务约定:''/undefined/null 均为「未选择」,归一为 naive 认可的空值
    const wrapper = mount(LmSelect, {
      props: { value: '', options: [{ label: 'A', value: 'a' }] },
    })
    const select = wrapper.findComponent(NSelect)
    expect(select.props('value')).toBeNull()

    const multi = mount(LmSelect, {
      props: { value: '', multiple: true, options: [] },
    })
    expect(multi.findComponent(NSelect).props('value')).toEqual([])
  })

  it('LmSelect 已有值不被动归一', () => {
    const wrapper = mount(LmSelect, {
      props: { value: 'a', options: [{ label: 'A', value: 'a' }] },
    })
    expect(wrapper.findComponent(NSelect).props('value')).toBe('a')
  })
})

describe('日期与树选择(挂载冒烟)', () => {
  it('LmDatePicker 渲染日期输入', () => {
    const wrapper = mountWithProvider(() => h(LmDatePicker, { placeholder: '选择日期' }))
    expect(wrapper.find('input').exists()).toBe(true)
  })

  it('LmTreeSelect 渲染占位', () => {
    const wrapper = mountWithProvider(() =>
      h(LmTreeSelect, { placeholder: '选择节点', options: [{ key: 'n1', label: '节点1' }] }),
    )
    expect(wrapper.find('.n-base-selection').exists()).toBe(true)
  })

  it('挂载场景下 ref 响应式驱动 value 回显(与 useForm 渲染路径一致)', async () => {
    const text = ref('')
    const wrapper = mountWithProvider(() => h(LmInput, { value: text.value }))
    text.value = 'linked'
    await nextTick()
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('linked')
  })
})
