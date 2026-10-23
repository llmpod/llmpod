/* eslint-disable @typescript-eslint/no-explicit-any */
import type {
  FormButtonSchema,
  FormComponentName,
  FormCustomSchema,
  FormFieldSchema,
  FormGroupSchema,
  FormItemConfig,
  FormOptions,
  FormTreeOptions,
} from './types'

type FieldExtra = Omit<FormItemConfig, 'attrs'> & {
  attrs?: Record<string, any>
}

function createField(
  component: FormComponentName,
  label: string,
  field: string,
  config: FieldExtra = {},
): FormFieldSchema {
  return { type: 'field', component, label, field, ...config }
}

export function FormInput(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmInput', label, field, config)
}

export function FormTextarea(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmTextarea', label, field, config)
}

export function FormPassword(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmPassword', label, field, config)
}

export function FormInputNumber(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmInputNumber', label, field, config)
}

export function FormSelect(
  label: string,
  field: string,
  options: FormOptions,
  config: FieldExtra = {},
) {
  return createField('LmSelect', label, field, {
    ...config,
    attrs: { clearable: true, ...config.attrs, options },
  })
}

export function FormTreeSelect(
  label: string,
  field: string,
  options: FormTreeOptions,
  config: FieldExtra = {},
) {
  return createField('LmTreeSelect', label, field, {
    ...config,
    attrs: { clearable: true, ...config.attrs, options },
  })
}

export function FormRadioGroup(
  label: string,
  field: string,
  options: FormOptions,
  config: FieldExtra = {},
) {
  return createField('LmRadioGroup', label, field, {
    ...config,
    attrs: { ...config.attrs, options },
  })
}

export function FormRadioButtonGroup(
  label: string,
  field: string,
  options: FormOptions,
  config: FieldExtra = {},
) {
  return createField('LmRadioGroup', label, field, {
    ...config,
    attrs: { button: true, ...config.attrs, options },
  })
}

export function FormCheckboxGroup(
  label: string,
  field: string,
  options: FormOptions,
  config: FieldExtra = {},
) {
  return createField('LmCheckboxGroup', label, field, {
    ...config,
    attrs: { ...config.attrs, options },
  })
}

export function FormSwitch(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmSwitch', label, field, config)
}

export function FormDatePicker(label: string, field: string, config: FieldExtra = {}) {
  return createField('LmDatePicker', label, field, config)
}

export function FormGroup(
  children: FormGroupSchema['children'],
  config: Omit<FormGroupSchema, 'type' | 'children'> = {},
): FormGroupSchema {
  return {
    type: 'group',
    children,
    gap: 12,
    wrap: false,
    attrs: { class: 'flex items-start', ...config.attrs },
    ...config,
  }
}

export function FormCustom(
  render: FormCustomSchema['render'],
  config: Omit<FormCustomSchema, 'type' | 'render'> = {},
): FormCustomSchema {
  return { type: 'custom', wrapFormItem: false, render, ...config }
}

export function FormButton(
  text: FormButtonSchema['text'],
  config: Omit<FormButtonSchema, 'type' | 'text'> = {},
): FormButtonSchema {
  return {
    type: 'button',
    text,
    attrs: { secondary: true, ...config.attrs },
    ...config,
  }
}
