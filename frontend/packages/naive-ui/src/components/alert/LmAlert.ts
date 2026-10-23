import { defineComponent, h, type PropType } from 'vue'
import { NAlert, alertProps, type AlertProps } from 'naive-ui'

/** LmAlert 的 type:在 naive-ui 基础上扩展 'primary'(主题色) */
export type LmAlertType = AlertProps['type'] | 'primary'

/** 与 alertProps 同构,仅放宽 type 联合 */
const lmAlertProps = {
  ...alertProps,
  type: {
    type: String as PropType<LmAlertType>,
    default: 'default',
  },
  bordered: {
    type: Boolean as PropType<boolean>,
    default: false,
  },
} as const

/**
 * n-alert 扩展包装:支持 type="primary"。
 * naive-ui 运行时按 createKey(key, type) 读取主题变量,五种类型的颜色变量已由主题覆盖统一注入
 * (见 @repo/hooks 的 theme/helper.ts getAlertOverrides);此处补齐组件类型 —
 * naive-ui 的 AlertProps 是 type 别名,无法通过模块合并扩展,故以包装组件放宽。
 */
export const LmAlert = defineComponent({
  name: 'LmAlert',
  props: lmAlertProps,
  setup(props, { attrs, slots }) {
    return () => h(NAlert, { ...attrs, ...props, type: props.type as AlertProps['type'] }, slots)
  },
})
