/* eslint-disable @typescript-eslint/no-explicit-any */
import { h, unref, type Component, type VNodeChild } from 'vue'
import { NButton, NSpace, type FormItemRule } from 'naive-ui'
import {
  LmFormItem,
  LmInput,
  LmTextarea,
  LmPassword,
  LmInputNumber,
  LmSelect,
  LmTreeSelect,
  LmRadioGroup,
  LmCheckboxGroup,
  LmSwitch,
  LmDatePicker,
} from '@repo/naive-ui'
import type { FormComponentName, FormFieldSchema, FormRenderContext, FormSchema } from './types'

// 惰性构建:hooks ↔ naive-ui 存在循环引用(LmLayout/LmDevThemeToggle 反向引用 @repo/hooks),
// 模块求值期读取 naive-ui 导出可能拿到未初始化值(视入口 import 顺序而定),
// 推迟到渲染期首次调用时构建,此时两侧模块均已完全初始化
let componentMap: Record<FormComponentName, Component> | null = null

function resolveFieldComponent(component: FormComponentName | Component): Component {
  if (typeof component !== 'string') return component
  componentMap ??= {
    LmInput,
    LmTextarea,
    LmPassword,
    LmInputNumber,
    LmSelect,
    LmTreeSelect,
    LmRadioGroup,
    LmCheckboxGroup,
    LmSwitch,
    LmDatePicker,
  }
  return componentMap[component]
}

/** 存在 rules 时查询是否包含 required,有则返回 true。 */
function hasRequiredRule(rules?: FormItemRule | FormItemRule[]): boolean | undefined {
  if (!rules) return undefined
  const list = Array.isArray(rules) ? rules : [rules]
  return list.some((rule) => rule.required)
}

/**
 * date-picker 配置 valueFormat 时,model 值语义为该格式的字符串,必须走
 * formatted-value 通道双向绑定:naive-ui 的 value 通道是 timestamp 语义,
 * 且空值仅接受 null —— 传入空串等非法值会在组件 setup 时 format(Invalid Date)
 * 抛 RangeError: Invalid time value(弹窗挂载即崩)。
 */
function resolveDateValueKeys(schema: FormFieldSchema): { valueKey: string; updateKey: string } {
  if (schema.component === 'LmDatePicker' && schema.attrs?.valueFormat) {
    // naive-ui doUpdateFormattedValue 按 camelCase prop key 直取 handler
    return { valueKey: 'formattedValue', updateKey: 'onUpdate:formattedValue' }
  }
  return { valueKey: 'value', updateKey: 'onUpdate:value' }
}

function renderField<T extends object>(
  schema: FormFieldSchema,
  ctx: FormRenderContext<T>,
): VNodeChild {
  const comp = resolveFieldComponent(schema.component)
  const { valueKey, updateKey } = resolveDateValueKeys(schema)
  return h(
    LmFormItem,
    {
      label: schema.label,
      path: schema.field,
      rule: schema.rules,
      showRequireMark: hasRequiredRule(schema.rules),
      ...schema.itemProps,
    },
    {
      default: () =>
        h(
          comp,
          {
            [valueKey]: (ctx.model as any)[schema.field],
            [updateKey]: (v: any) => ctx.setValues({ [schema.field]: v } as any),
            disabled: unref(schema.disabled),
            readonly: unref(schema.readonly),
            ...schema.attrs,
          },
          schema.controlSlots,
        ),
      // 透传 schema.slots 到 FormItem(feedback 等插槽由 naive-ui 原生支持)
      ...schema.slots,
    },
  )
}

export function renderSchema<T extends object>(
  schema: FormSchema,
  ctx: FormRenderContext<T>,
): VNodeChild {
  if ('visible' in schema && schema.visible !== undefined && unref(schema.visible) === false) {
    return null
  }

  if (schema.type === 'field') {
    return renderField(schema, ctx)
  }

  if (schema.type === 'group') {
    return h(
      'div',
      {
        ...schema.attrs,
        style: {
          display: 'flex',
          flexWrap: schema.wrap ? 'wrap' : 'nowrap',
          gap: typeof schema.gap === 'number' ? `${schema.gap}px` : schema.gap,
          ...schema.attrs?.style,
        },
      },
      schema.children.map((item) => renderSchema(item, ctx)),
    )
  }

  if (schema.type === 'custom') {
    const content = schema.render(ctx)
    if (!schema.wrapFormItem) return content
    return h(
      LmFormItem,
      {
        label: schema.label,
        path: schema.field,
        rule: schema.rules,
        showRequireMark: hasRequiredRule(schema.rules),
        ...schema.itemProps,
      },
      { default: () => content },
    )
  }

  if (schema.type === 'button') {
    return h(
      NButton,
      {
        ...schema.attrs,
        disabled: unref(schema.disabled),
        loading: unref(schema.loading),
        onClick: () => schema.onClick?.(ctx),
      },
      { default: () => unref(schema.text) },
    )
  }

  if (schema.type === 'actions') {
    return h(
      NSpace,
      { ...schema.attrs },
      { default: () => schema.children.map((btn) => renderSchema(btn, ctx)) },
    )
  }

  return null
}
