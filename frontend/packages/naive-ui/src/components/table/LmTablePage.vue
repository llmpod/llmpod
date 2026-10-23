<script setup lang="ts">
import { computed } from 'vue'
import { NPagination } from 'naive-ui'
import { useLmTableContext } from './context'

defineOptions({ name: 'LmTablePage', inheritAttrs: false })

interface Props {
  page?: number
  pageSize?: number
  total?: number
  pageSizes?: number[]
  showSizePicker?: boolean
  showQuickJumper?: boolean
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  pageSizes: () => [10, 20, 50, 100],
  showSizePicker: true,
  showQuickJumper: true,
  disabled: false,
})

const emit = defineEmits<{
  'update:page': [page: number]
  'update:pageSize': [pageSize: number]
  'change': [page: number, pageSize: number]
}>()

const ctx = useLmTableContext()

// inject 优先、props 次之、默认值最后
const innerPage = computed(() => props.page ?? ctx?.page.value ?? 1)
const innerPageSize = computed(() => props.pageSize ?? ctx?.pageSize.value ?? 20)
const innerTotal = computed(() => props.total ?? ctx?.total.value ?? 0)

/** 总页数:每页条数为 0 时兜底 1,避免除零与「第 1/0 页」 */
const totalPages = computed(() =>
  Math.max(1, Math.ceil(innerTotal.value / (innerPageSize.value || 1))),
)

const hasPrev = computed(() => !props.disabled && innerPage.value > 1)
const hasNext = computed(() => !props.disabled && innerPage.value < totalPages.value)

function handlePageChange(p: number) {
  ctx?.onPageChange(p)
  emit('update:page', p)
  emit('change', p, innerPageSize.value)
}

function handlePageSizeChange(ps: number) {
  // 修改每页条数由上下文(hooks 层)负责重置到第 1 页
  ctx?.onPageSizeChange(ps)
  emit('update:pageSize', ps)
  emit('change', 1, ps)
}
</script>

<template>
  <!-- 窄屏换形态:390px 下快速跳页与每页条数选择器没有位置,
       改为「上一页 / 第 N/M 页 · 共 T 条 / 下一页」,命中区 ≥44px。
       用 CSS 媒体查询切换两套 DOM,断点与 MOBILE_QUERY(≤767px)一致。 -->
  <div class="lm-table-page">
    <div class="lm-table-page__desktop">
      <n-pagination
        v-bind="$attrs"
        :page="innerPage"
        :page-size="innerPageSize"
        :item-count="innerTotal"
        :page-sizes="pageSizes"
        :show-size-picker="showSizePicker"
        :show-quick-jumper="showQuickJumper"
        :disabled="disabled"
        @update:page="handlePageChange"
        @update:page-size="handlePageSizeChange"
      >
        <!-- 总数常驻前缀(与移动形态「共 T 条」同口径);页面显式传 #prefix 时以页面为准 -->
        <template
          v-if="!$slots.prefix"
          #prefix
        >
          <span class="lm-table-page__total">共 {{ innerTotal }} 条</span>
        </template>
        <template
          v-for="(_, name) in $slots"
          #[name]="slotData"
          :key="name"
        >
          <slot
            :name="name"
            v-bind="slotData ?? {}"
          />
        </template>
      </n-pagination>
    </div>

    <div
      v-if="innerTotal > 0 && totalPages > 1"
      class="lm-table-page__mobile"
    >
      <button
        type="button"
        class="lm-table-page__btn"
        :disabled="!hasPrev"
        @click="handlePageChange(innerPage - 1)"
      >
        上一页
      </button>
      <span class="lm-table-page__meta">
        第 <b>{{ innerPage }}</b> / {{ totalPages }} 页 · 共 <b>{{ innerTotal }}</b> 条
      </span>
      <button
        type="button"
        class="lm-table-page__btn"
        :disabled="!hasNext"
        @click="handlePageChange(innerPage + 1)"
      >
        下一页
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 1rem = 10px(reset.scss: html font-size 62.5%) */

.lm-table-page__total {
  color: var(--c-default);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.lm-table-page__mobile {
  display: none;
}

@media (max-width: 767px) {
  .lm-table-page__desktop {
    display: none;
  }

  .lm-table-page__mobile {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
    width: 100%;
  }

  .lm-table-page__btn {
    flex: 0 0 auto;
    min-height: 4.4rem;
    padding: 0 1.4rem;
    border: 1px solid var(--c-line-strong);
    border-radius: 0.9rem;
    background: var(--c-fill-1);
    color: var(--c-strong);
    font-size: 1.3rem;
    transition: border-color 0.15s ease;
  }

  .lm-table-page__btn:not(:disabled):active {
    border-color: var(--c-primary);
  }

  .lm-table-page__btn:disabled {
    color: var(--c-secondary);
    cursor: not-allowed;
  }

  .lm-table-page__btn:focus-visible {
    outline: 2px solid var(--c-primary);
    outline-offset: 1px;
  }

  .lm-table-page__meta {
    min-width: 0;
    font-size: 1.2rem;
    color: var(--c-default);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }

  .lm-table-page__meta b {
    color: var(--c-strong);
    font-weight: 600;
  }
}
</style>
