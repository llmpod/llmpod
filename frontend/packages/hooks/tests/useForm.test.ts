// @vitest-environment happy-dom
import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { NDatePicker } from 'naive-ui'
import { describe, expect, it, vi } from 'vitest'
import {
  FormButton,
  FormCustom,
  FormDatePicker,
  FormGroup,
  FormInput,
  FormSelect,
  FormSwitch,
  useForm,
} from '../src/components/form'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

// ---------- 逻辑层(不挂载) ----------

describe('useForm 状态管理', () => {
  it('params 返回响应式 model,初始值来自 data()', () => {
    const api = useForm({ data: () => ({ name: 'a', count: 1 }), options: () => [] })
    expect(api.params()).toEqual({ name: 'a', count: 1 })
    // model 为 reactive:外部修改经 params 可见
    api.params().name = 'b'
    expect(api.params().name).toBe('b')
  })

  it('formOptions 支持 computed/ref 与普通数组', () => {
    const options = ref([])
    const api = useForm({
      data: () => ({}),
      options: () => options,
    })
    expect(api.formOptions.value).toEqual([])
    options.value = [FormInput('名称', 'name')]
    expect(api.formOptions.value).toHaveLength(1)
  })

  it('reset 恢复初始值,setInitialValues 更新基线', () => {
    const api = useForm({ data: () => ({ name: 'init' }), options: () => [] })
    api.params().name = 'changed'
    api.reset()
    expect(api.params().name).toBe('init')

    // 用户编辑后的值可固化为新基线(编辑表单回显场景)
    api.params().name = 'edited'
    api.setInitialValues({ name: 'edited' })
    api.reset()
    expect(api.params().name).toBe('edited')
  })

  it('嵌套值深隔离:表单内原地变更不污染上游数据(UF-02)', () => {
    const upstream = { tags: ['a'], nested: { count: 1 } }
    const api = useForm({ data: () => upstream, options: () => [] })

    api.params().tags.push('b')
    api.params().nested.count = 99
    expect(upstream.tags).toEqual(['a'])
    expect(upstream.nested.count).toBe(1)
  })

  it('嵌套值深隔离:多次编辑+reset 均恢复原始值,基线不被别名污染(UF-02)', () => {
    const api = useForm({
      data: () => ({ tags: ['a'] as string[], nested: { count: 1 } }),
      options: () => [],
    })

    api.params().tags.push('b')
    api.params().nested.count = 99
    api.reset()
    expect(api.params().tags).toEqual(['a'])
    expect(api.params().nested.count).toBe(1)

    // reset 回填后再编辑、再 reset:若回填产生别名,此次编辑会污染基线
    api.params().tags.push('c')
    api.params().nested.count = 100
    api.reset()
    expect(api.params().tags).toEqual(['a'])
    expect(api.params().nested.count).toBe(1)
  })

  it('setInitialValues 深快照:传入的嵌套引用不与基线别名(UF-02)', () => {
    const external = { ops: ['x'] }
    const api = useForm({ data: () => ({ ops: [] as string[] }), options: () => [] })

    api.setInitialValues({ ops: external.ops })
    external.ops.push('y')
    api.reset()
    expect(api.params().ops).toEqual(['x'])
  })

  it('data() 仅调用一次(非纯 data 不重复触发副作用)', () => {
    const data = vi.fn(() => ({ name: 'a' }))
    useForm({ data, options: () => [] })
    expect(data).toHaveBeenCalledTimes(1)
  })

  it('submit 有重入保护,submitting 在执行期间为 true', async () => {
    const gate = deferred<void>()
    const submit = vi.fn(() => gate.promise)
    const api = useForm({ data: () => ({}), options: () => [], submit })

    const first = api.submit()
    expect(api.submitting.value).toBe(true)
    const second = api.submit() // 重入被忽略
    gate.resolve()
    await Promise.all([first, second])

    expect(submit).toHaveBeenCalledTimes(1)
    expect(api.submitting.value).toBe(false)
  })

  it('submit 回调收到 (model, validate),异常时 submitting 复位', async () => {
    const submit = vi.fn(async () => {
      throw new Error('boom')
    })
    const api = useForm({ data: () => ({ name: 'x' }), options: () => [], submit })

    await expect(api.submit()).rejects.toThrow('boom')
    expect(api.submitting.value).toBe(false)

    await api.submit().catch(() => {})
    const [model, validate] = submit.mock.calls[1]
    expect(model).toEqual({ name: 'x' })
    expect(typeof validate).toBe('function')
  })
})

// ---------- 字段工厂 schema ----------

describe('字段工厂', () => {
  it('基础字段:type=field 且映射正确组件名', () => {
    expect(FormInput('名称', 'name')).toMatchObject({ type: 'field', component: 'LmInput', label: '名称', field: 'name' })
    expect(FormSwitch('启用', 'enabled').component).toBe('LmSwitch')
  })

  it('FormSelect 默认 clearable 且注入 options,attrs 可覆盖默认', () => {
    const schema = FormSelect('引擎', 'engine', [
      { label: 'vLLM', value: 'vllm' },
    ])
    expect(schema.attrs).toMatchObject({ clearable: true, options: [{ label: 'vLLM', value: 'vllm' }] })

    const strict = FormSelect('引擎', 'engine', [], { attrs: { clearable: false, placeholder: '必选' } })
    expect(strict.attrs.clearable).toBe(false)
    expect(strict.attrs.placeholder).toBe('必选')
  })

  it('FormGroup 默认 gap/wrap/class,FormCustom 默认不包 FormItem,FormButton 默认 secondary', () => {
    const group = FormGroup([FormInput('a', 'a')])
    expect(group).toMatchObject({ type: 'group', gap: 12, wrap: false })
    expect(group.attrs?.class).toBe('flex items-start')

    expect(FormCustom(() => h('div')).wrapFormItem).toBe(false)
    expect(FormButton('重置').attrs).toMatchObject({ secondary: true })
  })
})

// ---------- 挂载集成 ----------

describe('useForm 渲染与校验(挂载)', () => {
  function setup(options: Parameters<typeof useForm>[0]) {
    const api = useForm(options)
    const wrapper = mount(api.LmForm, { attachTo: document.body })
    return { api, wrapper }
  }

  it('渲染 label 与控件,输入经 value/onUpdate:value 写回 model', async () => {
    const { api, wrapper } = setup({
      data: () => ({ name: '' }),
      options: () => [FormInput('名称', 'name')],
    })
    expect(wrapper.text()).toContain('名称')

    const input = wrapper.find('input')
    await input.setValue('typed')
    expect(api.params().name).toBe('typed')

    // 外部 setValues 回显到 DOM
    api.params().name = 'from-code'
    await nextTick()
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('from-code')
    wrapper.unmount()
  })

  it('visible:false 的字段不渲染', () => {
    const { wrapper } = setup({
      data: () => ({ a: '', b: '' }),
      options: () => [
        FormInput('A', 'a'),
        { ...FormInput('B', 'b'), visible: false },
      ],
    })
    expect(wrapper.text()).toContain('A')
    expect(wrapper.text()).not.toContain('B')
    wrapper.unmount()
  })

  it('必填校验:空值 reject,填值 resolve', async () => {
    const { api, wrapper } = setup({
      data: () => ({ name: '' }),
      options: () => [
        FormInput('名称', 'name', { rules: { required: true, message: '必填项' } }),
      ],
    })
    await expect(api.validate()).rejects.toBeTruthy()

    const input = wrapper.find('input')
    await input.setValue('ok')
    await expect(api.validate()).resolves.toBeUndefined()
    wrapper.unmount()
  })

  it('number 值必填须显式 type:number(已知坑回归锁定)', async () => {
    const { api, wrapper } = setup({
      data: () => ({ modelId: null as number | null }),
      options: () => [
        FormSelect('模型', 'modelId', [{ label: 'M', value: 1 }], {
          rules: { required: true, type: 'number', message: '请选择模型' },
        }),
      ],
    })
    await expect(api.validate()).rejects.toBeTruthy()

    api.params().modelId = 1
    await nextTick()
    await expect(api.validate()).resolves.toBeUndefined()
    wrapper.unmount()
  })

  it('required 规则展示必填星标(showRequireMark)', () => {
    const { wrapper } = setup({
      data: () => ({ a: '', b: '' }),
      options: () => [
        FormInput('必填', 'a', { rules: { required: true } }),
        FormInput('选填', 'b'),
      ],
    })
    const marks = wrapper.findAll('.n-form-item-label__asterisk')
    expect(marks).toHaveLength(1)
    wrapper.unmount()
  })

  it('disableOnSubmitting:提交期间表单禁用,结束恢复', async () => {
    const gate = deferred<void>()
    const { api, wrapper } = setup({
      data: () => ({ name: '' }),
      options: () => [FormInput('名称', 'name')],
      submit: () => gate.promise,
    })

    const input = wrapper.find('input')
    expect((input.element as HTMLInputElement).disabled).toBe(false)

    const pending = api.submit()
    await nextTick()
    expect((wrapper.find('input').element as HTMLInputElement).disabled).toBe(true)

    gate.resolve()
    await pending
    await nextTick()
    expect((wrapper.find('input').element as HTMLInputElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('FormButton onClick 收到渲染上下文(model 可读写)', async () => {
    const onClick = vi.fn()
    const { wrapper } = setup({
      data: () => ({ name: 'n' }),
      options: () => [FormButton('清空', { onClick })],
    })
    await wrapper.find('button').trigger('click')
    expect(onClick).toHaveBeenCalledTimes(1)
    const ctx = onClick.mock.calls[0][0]
    expect(ctx.model).toEqual({ name: 'n' })
    ctx.setValues({ name: 'cleared' })
    expect(ctx.getValues().name).toBe('cleared')
    wrapper.unmount()
  })

  it('FormCustom 渲染函数收到上下文且不包 FormItem', () => {
    const render = vi.fn(() => h('div', { class: 'custom-block' }, '自定义'))
    const { wrapper } = setup({
      data: () => ({}),
      options: () => [FormCustom(render)],
    })
    expect(wrapper.find('.custom-block').exists()).toBe(true)
    expect(wrapper.find('.n-form-item').exists()).toBe(false)
    expect(render).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('controlSlots 透传给字段控件本身(如 LmSelect 的 action 引导)', () => {
    const StubControl = defineComponent({
      name: 'StubControl',
      setup(_, { slots }) {
        return () => h('div', { class: 'stub-control' }, slots.action?.())
      },
    })
    const { wrapper } = setup({
      data: () => ({ name: '' }),
      options: () => [
        {
          ...FormInput('名称', 'name'),
          component: StubControl,
          controlSlots: { action: () => h('span', { class: 'slot-guide' }, '去添加') },
        },
      ],
    })
    expect(wrapper.find('.stub-control').exists()).toBe(true)
    expect(wrapper.find('.slot-guide').text()).toBe('去添加')
    wrapper.unmount()
  })

  it('FormDatePicker 配置 valueFormat 时走 formatted-value 通道(空值 null 不触发 naive-ui Invalid time value)', async () => {
    const { api, wrapper } = setup({
      data: () => ({ expires_at: null as string | null }),
      options: () => [
        FormDatePicker('过期时间', 'expires_at', {
          attrs: { type: 'datetime', valueFormat: "yyyy-MM-dd'T'HH:mm:ss" },
        }),
      ],
    })
    const picker = wrapper.findComponent(NDatePicker)
    // 值经 formattedValue 通道下发,value 通道不注入
    expect(picker.props('formattedValue')).toBe(null)
    expect(picker.props('value')).toBe(undefined)

    // naive-ui 更新 formatted value → model 收到格式化字符串
    picker.vm.$emit('update:formattedValue', '2026-12-31T00:00:00')
    await nextTick()
    expect(api.params().expires_at).toBe('2026-12-31T00:00:00')
    expect(picker.props('formattedValue')).toBe('2026-12-31T00:00:00')

    // 清空 → model 回到 null
    picker.vm.$emit('update:formattedValue', null)
    await nextTick()
    expect(api.params().expires_at).toBe(null)
    wrapper.unmount()
  })

  it('FormDatePicker 未配置 valueFormat 时保持 value 通道(timestamp 回归锁定)', async () => {
    const { api, wrapper } = setup({
      data: () => ({ at: null as number | null }),
      options: () => [FormDatePicker('时间', 'at', { attrs: { type: 'datetime' } })],
    })
    const picker = wrapper.findComponent(NDatePicker)
    expect(picker.props('value')).toBe(null)

    picker.vm.$emit('update:value', 1759200000000)
    await nextTick()
    expect(api.params().at).toBe(1759200000000)
    wrapper.unmount()
  })
})
