<script setup lang="ts">
import { computed } from 'vue'

defineOptions({ name: 'LmTableGroup', inheritAttrs: false })

interface Props {
  /** 内容区是否带边框 */
  bordered?: boolean
  /** 表头是否带底分割线 */
  headerBordered?: boolean
  /** 三段(header/content/footer)之间的间距 */
  gap?: number | string
}

const props = withDefaults(defineProps<Props>(), {
  bordered: false,
  headerBordered: false,
  gap: 0,
})

const gapStyle = computed(() => (typeof props.gap === 'number' ? `${props.gap}px` : props.gap))
</script>

<template>
  <section class="flex flex-col" :style="{ gap: gapStyle }">
    <header
      v-if="$slots['header-left'] || $slots['header-right']"
      class="lm-table-group-header flex flex-wrap items-center justify-between gap-[.8rem] pb-[1.2rem] rounded-[.6rem]"
      :class="headerBordered ? 'border-b border-line' : ''"
    >
      <div class="flex flex-wrap items-center gap-[.8rem] min-w-0">
        <slot name="header-left" />
      </div>
      <div class="flex flex-wrap items-center gap-[.8rem] min-w-0">
        <slot name="header-right" />
      </div>
    </header>

    <div class="bg-fill-1 rounded-[.6rem]" :class="bordered ? 'border border-line' : ''">
      <slot name="content" />
    </div>

    <footer
      v-if="$slots['footer-left'] || $slots['footer-right']"
      class="lm-table-group-footer flex flex-wrap items-center justify-between gap-[.8rem] py-[1.2rem]"
    >
      <div class="flex flex-wrap items-center gap-[.8rem] min-w-0">
        <slot name="footer-left" />
      </div>
      <div class="flex flex-wrap items-center gap-[.8rem] min-w-0">
        <slot name="footer-right" />
      </div>
    </footer>
  </section>
</template>

<style>
/* 窄屏(≤767px)堆叠与控件钳宽:slot 内容不属于本组件 scope,用带唯一前缀的全局类命中。
 * flex: 1 1 100% 让 header/footer 两段各自占满整行;max-width: 100% 钳制页面写的
 * !w-[28rem] 等固定宽控件(max-width 对 width 的钳制不受 important 影响)。 */
@media (max-width: 767px) {
  .lm-table-group-header > div,
  .lm-table-group-footer > div {
    flex: 1 1 100%;
    min-width: 0;
  }
  .lm-table-group-header > div > *,
  .lm-table-group-footer > div > * {
    max-width: 100%;
  }
  /* 分页器窄屏内部换行,避免页码+每页条数横向溢出 */
  .lm-table-group-footer .n-pagination {
    flex-wrap: wrap;
    row-gap: 0.4rem;
  }
}
</style>
