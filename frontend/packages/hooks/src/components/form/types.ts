/* eslint-disable @typescript-eslint/no-explicit-any */
import { defineComponent } from 'vue'
import type { Component, ComputedRef, Ref, VNodeChild } from 'vue'
import type { FormInst, FormItemRule, TreeSelectOption } from 'naive-ui'

export type MaybeRef<T> = T | Ref<T> | ComputedRef<T>
export type FormFieldKey<T extends object> = Extract<keyof T, string>

export type FormComponentName =
  | 'LmInput'
  | 'LmTextarea'
  | 'LmPassword'
  | 'LmInputNumber'
  | 'LmSelect'
  | 'LmTreeSelect'
  | 'LmRadioGroup'
  | 'LmCheckboxGroup'
  | 'LmSwitch'
  | 'LmDatePicker'

export interface FormItemConfig {
  attrs?: Record<string, any>
  itemProps?: Record<string, any>
  rules?: FormItemRule | FormItemRule[]
  visible?: MaybeRef<boolean>
  disabled?: MaybeRef<boolean>
  readonly?: MaybeRef<boolean>
  slots?: Record<string, (...args: any[]) => VNodeChild>
  /**
   * 透传给字段控件本身的插槽(区别于 slots 透传给 LmFormItem),
   * 如 LmSelect(n-select) 的 header/action 插槽,可用于下拉底部操作引导。
   */
  controlSlots?: Record<string, (...args: any[]) => VNodeChild>
}

export interface FormFieldSchema extends FormItemConfig {
  type: 'field'
  field: string
  label?: string
  component: FormComponentName | Component
}

export interface FormGroupSchema {
  type: 'group'
  children: FormSchema[]
  attrs?: Record<string, any>
  gap?: number | string
  wrap?: boolean
  visible?: MaybeRef<boolean>
}

export interface FormCustomSchema extends Omit<FormItemConfig, 'rules'> {
  type: 'custom'
  field?: string
  label?: string
  wrapFormItem?: boolean
  rules?: FormItemRule | FormItemRule[]
  render: (context: FormRenderContext<any>) => VNodeChild
}

export interface FormButtonSchema {
  type: 'button'
  key?: string
  text: MaybeRef<string>
  attrs?: Record<string, any>
  visible?: MaybeRef<boolean>
  disabled?: MaybeRef<boolean>
  loading?: MaybeRef<boolean>
  onClick?: (context: FormRenderContext<any>) => void | Promise<void>
}

export interface FormActionsSchema {
  type: 'actions'
  children: FormButtonSchema[]
  attrs?: Record<string, any>
  visible?: MaybeRef<boolean>
}

export type FormSchema =
  | FormFieldSchema
  | FormGroupSchema
  | FormCustomSchema
  | FormButtonSchema
  | FormActionsSchema

export interface FormRenderContext<T extends object> {
  model: T
  validate: (fields?: FormFieldKey<T>[]) => Promise<void>
  reset: () => void
  submit: () => Promise<void>
  setValues: (values: Partial<T>) => void
  getValues: () => T
  submitting: Readonly<Ref<boolean>>
}

export type FormOptions = MaybeRef<Array<Record<string, any>>>
export type FormTreeOptions = MaybeRef<TreeSelectOption[]>

export interface UseFormOptions<T extends object> {
  /** 初始表单数据,仅在 useForm 初始化时执行一次。 */
  data: () => T
  /** 字段配置;可直接返回数组,也可返回 computed/ref。 */
  options: () => FormSchema[] | ComputedRef<FormSchema[]> | Ref<FormSchema[]>
  /** 提交回调;validate 校验失败会 reject,后续不执行。 */
  submit?: (model: Readonly<T>, validate: () => Promise<void>) => Promise<void> | void
  /** 透传至 LmForm / n-form 的属性。 */
  formProps?: Record<string, any>
  /** 表单布局,例如 labelPlacement、labelWidth。 */
  layout?: Record<string, any>
  /** 提交期间是否自动禁用表单,默认 true。 */
  disableOnSubmitting?: boolean
}

export interface UseFormReturn<T extends object> {
  LmForm: ReturnType<typeof defineComponent>
  formRef: Ref<FormInst | null>
  formOptions: ComputedRef<FormSchema[]>
  submit: () => Promise<void>
  validate: (fields?: FormFieldKey<T>[]) => Promise<void>
  reset: () => void
  params: () => T
  setValues: (values: Partial<T>) => void
  setInitialValues: (values: Partial<T>) => void
  submitting: Ref<boolean>
}
