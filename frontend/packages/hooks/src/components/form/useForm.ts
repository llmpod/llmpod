import { computed, defineComponent, h, reactive, ref, toRaw, unref } from 'vue'
import type { FormInst } from 'naive-ui'
import { LmForm } from '@repo/naive-ui'
import { renderSchema } from './render'
import type { FormRenderContext, UseFormOptions, UseFormReturn } from './types'

/** 深快照:剥离响应式代理后深拷贝,嵌套对象/数组与源断链(reset 基准不可被表单内原地变更污染) */
function snapshotDeep<T>(value: T): T {
  return structuredClone(toRawDeep(value)) as T
}

/** 递归剥离响应式代理:structuredClone 无法克隆 proxy,须先层层还原为普通对象树 */
function toRawDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(toRawDeep)
  if (value instanceof Date || value instanceof RegExp) return value
  if (value && typeof value === 'object') {
    const raw = toRaw(value) as Record<string, unknown>
    const out: Record<string, unknown> = {}
    for (const key of Object.keys(raw)) out[key] = toRawDeep(raw[key])
    return out
  }
  return value
}

export function useForm<T extends object>(options: UseFormOptions<T>): UseFormReturn<T> {
  const {
    data,
    options: optionsFn,
    submit,
    formProps = {},
    layout = {},
    disableOnSubmitting = true,
  } = options

  // data() 只调用一次;model 与 reset 基准各持独立深快照,与上游(行数据/store)引用断链
  const source = data()
  const model = reactive(snapshotDeep(source)) as T
  const initialValues = snapshotDeep(source)
  const formRef = ref<FormInst | null>(null)
  const submitting = ref(false)

  const formOptions = computed(() => unref(optionsFn()))

  const params = () => model

  const setValues = (values: Partial<T>) => {
    Object.assign(model, values)
  }

  const getValues = () => model

  // naive FormInst.validate 不支持按 path 过滤,当前为全表校验
  const validate = async () => {
    await formRef.value!.validate()
  }

  const reset = () => {
    // 深回填:嵌套值拷贝新引用,避免 model 与 initialValues 产生别名
    // (别名会让下一轮编辑污染 reset 基准,第二次 reset 恢复的是污染值)
    Object.assign(model, snapshotDeep(initialValues))
    formRef.value?.restoreValidation()
  }

  const setInitialValues = (values: Partial<T>) => {
    // 深快照入基准:调用方传入的嵌套引用(如行数据的数组)不进 initialValues
    Object.assign(initialValues, snapshotDeep(values))
  }

  const submitFn = async () => {
    if (submitting.value) return
    submitting.value = true
    try {
      await submit?.(model, validate)
    } finally {
      submitting.value = false
    }
  }

  const ctx: FormRenderContext<T> = {
    model,
    validate,
    reset,
    submit: submitFn,
    setValues,
    getValues,
    submitting,
  }

  const LmFormComponent = defineComponent({
    name: 'UseFormLmForm',
    render() {
      return h(
        LmForm,
        {
          ref: formRef,
          model,
          disabled: disableOnSubmitting && submitting.value ? true : undefined,
          ...layout,
          ...formProps,
        },
        {
          default: () => formOptions.value.map((schema) => renderSchema(schema, ctx)),
        },
      )
    },
  })

  return {
    LmForm: LmFormComponent,
    formRef,
    formOptions,
    submit: submitFn,
    validate,
    reset,
    params,
    setValues,
    setInitialValues,
    submitting,
  }
}
