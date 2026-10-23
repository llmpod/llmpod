import { defineComponent, h } from 'vue'
import { NCard, cardProps } from 'naive-ui'

/** 与 cardProps 同构,仅将 bordered 默认值改为 false */
const lmCardProps = {
  ...cardProps,
  bordered: {
    type: Boolean,
    default: false,
  },
} as const

/**
 * n-card 包装:默认不显示边框(bordered=false),需要边框时显式传 bordered。
 * 模板中 <LmCard> 由 LmResolver 自动引入,无需 import。
 */
export const LmCard = defineComponent({
  name: 'LmCard',
  inheritAttrs: false,
  props: lmCardProps,
  setup(props, { attrs, slots }) {
    return () => h(NCard, { ...attrs, ...props }, slots)
  },
})
