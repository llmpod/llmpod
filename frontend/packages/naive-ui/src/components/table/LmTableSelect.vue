<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NDropdown, NIcon } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import { ChevronDown } from '@vicons/ionicons5'
import type { DataTableRowData } from 'naive-ui'
import type { LmSelectAction } from './types'
import {
  CELL_ACTION_GAP,
  fallbackInlineCount,
  resolveCardInlineCount,
  useLmCardActionBudget,
} from './action-budget'

defineOptions({ name: 'LmTableSelect', inheritAttrs: false })

interface Props {
  /** 操作项 */
  actions: LmSelectAction[]
  /** 行数据(行内操作时传入) */
  row?: DataTableRowData
  /** 主操作按钮之间的间距 */
  gap?: number | string
  /** "更多"入口文字 */
  moreText?: string
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  gap: CELL_ACTION_GAP,
  moreText: '更多',
})

const emit = defineEmits<{
  select: [action: LmSelectAction, row?: DataTableRowData]
}>()

type MaybeFn<T> = T | ((row: DataTableRowData) => T)

function resolve<T>(value: MaybeFn<T> | undefined, fallback: T): T {
  if (typeof value === 'function')
    return (value as (row: DataTableRowData) => T)(props.row as DataTableRowData)
  return value ?? fallback
}

function isDisabled(action: LmSelectAction): boolean {
  return props.disabled || resolve(action.disabled, false)
}

async function runAction(action: LmSelectAction, event?: MouseEvent) {
  // 同一操作在途时拒绝重入(loading 态 naive-ui 已拦 click,这里兜底程序化触发)
  if (isDisabled(action) || isLoading(action)) return
  // 鼠标点击后立即让出焦点:n-modal/n-drawer 内部的 VFocusTrap 打开时记录
  // document.activeElement,关闭时默认把焦点还给它 —— 不让出,文字按钮会在
  // 弹窗/抽屉关闭后被重新聚焦(focus ring 残留、回车误触重开)。键盘触发
  // (click.detail=0)不动焦点,保留"关闭后回到触发元素"的无障碍惯例。
  if (event && event.detail > 0) {
    ;(document.activeElement as HTMLElement | null)?.blur()
  }
  const row = props.row as DataTableRowData
  if (action.beforeConfirm) {
    const ok = await action.beforeConfirm(row)
    if (!ok) return
  }
  // 异步 onClick(如行操作接口请求)按操作项粒度跟踪 loading:
  // 对应按钮转 loading 并拦截连点,其余操作互不影响;同步 onClick 零开销。
  const result = action.onClick?.(row)
  if (result instanceof Promise) {
    runningKey.value = action.key || action.title
    try {
      await result
    } catch {
      // 点击上下文无人接异常;错误提示归业务/HTTP 层,这里只负责恢复按钮
    } finally {
      runningKey.value = null
    }
  }
  emit('select', action, row)
}

/** 正在执行的操作项标识(key 缺省取 title);null = 空闲 */
const runningKey = ref<string | null>(null)

/** 指定操作项是否在 loading(仅作用自身按钮,不影响其余操作) */
function isLoading(action: LmSelectAction): boolean {
  return runningKey.value === (action.key || action.title)
}

/* ── 内联预算(仅窄屏卡片) ──────────────────────────────────────────
   收纳(行内截断 + 「更多」)只属于窄屏卡片:操作项超过卡宽预算自动下沉到
   「更多」,调用方不需要声明 more —— 漏配一次就白搭的约定不算约定。
   桌面操作列全量平铺不收纳;已声明的 more.children 仍然生效,走「更多」下拉。 */

/** 卡片上下文:存在即代表窄屏卡片 —— 「更多」走底部动作面板而非下拉
 * (卡片挂在 overflow-y:auto 的滚动容器里,placement="bottom-end" 的弹层会被裁切) */
const cardBudget = useLmCardActionBudget()

/** 可见操作(排除 more 入口;show 支持函数式配置,按行求值) */
const visibleActions = computed(() => props.actions.filter((a) => !a.more && resolve(a.show, true)))

/** 显式声明的 more 子项 */
const declaredChildren = computed(() =>
  (props.actions.find((a) => a.more)?.children ?? []).filter((a) => resolve(a.show, true)),
)

/** 卡片内间距由卡片指定(预算与渲染必须同值);表格沿用 gap prop */
const gapStyle = computed(() => {
  if (cardBudget?.actionGap != null) return `${cardBudget.actionGap}px`
  return typeof props.gap === 'number' ? `${props.gap}px` : props.gap
})

/** 预算用的数值间距:rem 按 1rem = 10px 折算(reset.scss) */
const numericGap = computed(() => {
  if (cardBudget?.actionGap != null) return cardBudget.actionGap
  if (typeof props.gap === 'number') return props.gap
  const value = Number.parseFloat(props.gap)
  if (!Number.isFinite(value)) return CELL_ACTION_GAP
  return props.gap.trim().endsWith('rem') ? value * 10 : value
})

/** 内联项数:卡片按实测卡宽切预算;桌面全量平铺(仅显式 more 项走下拉) */
const inlineCount = computed(() => {
  if (!cardBudget) return visibleActions.value.length
  const labels = visibleActions.value.map((a) => a.title)
  const gap = numericGap.value
  const width = cardBudget.cardWidth.value
  return width
    ? resolveCardInlineCount(width, labels, cardBudget.hasToggle.value, gap)
    : fallbackInlineCount(labels.length)
})

const inlineActions = computed(() => visibleActions.value.slice(0, inlineCount.value))

/** 溢出项:自动下沉的尾部(保持声明顺序)+ 显式 more.children */
const overflowActions = computed(() => [
  ...visibleActions.value.slice(inlineCount.value),
  ...declaredChildren.value,
])

const hasOverflow = computed(() => overflowActions.value.length > 0)
const safeOverflow = computed(() => overflowActions.value.filter((a) => !a.danger))
const dangerOverflow = computed(() => overflowActions.value.filter((a) => a.danger))

/* ── 桌面/表格单元格:下拉「更多」(仅显式声明的 more 项) ─────────── */
const moreOptions = computed<DropdownOption[]>(() =>
  overflowActions.value.map((a) => ({
    key: a.key || a.title,
    disabled: isDisabled(a) || isLoading(a),
    label: () => h('span', { class: a.danger ? 'text-error' : '' }, a.title),
  })),
)

function handleMoreSelect(key: string | number) {
  const action = overflowActions.value.find((a) => (a.key || a.title) === key)
  if (action) void runAction(action)
}

/* ── 窄屏卡片:底部动作面板 ─────────────────────────────────────── */
const sheetShow = ref(false)
/** 面板副标题:写明"这是对哪一行",危险操作前多一次定位 */
const sheetSubtitle = computed(() => {
  const label = cardBudget?.rowLabel?.(props.row)
  return label ?? ''
})

let lastActive: HTMLElement | null = null
let bodyOverflow = ''

function openSheet() {
  if (!hasOverflow.value) return
  lastActive = (document.activeElement as HTMLElement | null) ?? null
  sheetShow.value = true
}

function closeSheet() {
  if (!sheetShow.value) return
  sheetShow.value = false
  lastActive?.focus?.()
  lastActive = null
}

function runFromSheet(action: LmSelectAction) {
  closeSheet()
  void runAction(action)
}

function onSheetKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeSheet()
}

// 面板打开时锁滚动并接管 Esc;关闭后原样还原,不留副作用
watch(sheetShow, (show) => {
  if (typeof document === 'undefined') return
  if (show) {
    bodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onSheetKeydown)
  }
  else {
    document.body.style.overflow = bodyOverflow
    document.removeEventListener('keydown', onSheetKeydown)
  }
})

/* ── 生命周期清理(卡片宽度由 LmTableCard 提供,桌面不测量) ────────── */
onBeforeUnmount(() => {
  if (typeof document === 'undefined') return
  document.removeEventListener('keydown', onSheetKeydown)
  if (sheetShow.value) document.body.style.overflow = bodyOverflow
})
</script>

<template>
  <div
    v-bind="$attrs"
    class="lm-table-select flex items-center justify-end"
    :style="{ gap: gapStyle }"
  >
    <n-button
      v-for="action in inlineActions"
      :key="action.key || action.title"
      text
      :type="action.danger ? 'error' : 'primary'"
      class="text-base"
      :loading="isLoading(action)"
      :disabled="isDisabled(action)"
      @click="runAction(action, $event)"
    >
      {{ action.title }}
    </n-button>

    <!-- 表格单元格:下拉;窄屏卡片改走底部动作面板 -->
    <n-dropdown
      v-if="hasOverflow && !cardBudget"
      :options="moreOptions"
      trigger="click"
      placement="bottom-end"
      @select="handleMoreSelect"
    >
      <n-button
        text
        type="primary"
        icon-placement="right"
        :disabled="disabled"
      >
        {{ moreText }}
        <template #icon>
          <n-icon><ChevronDown /></n-icon>
        </template>
      </n-button>
    </n-dropdown>

    <n-button
      v-else-if="hasOverflow"
      text
      type="primary"
      icon-placement="right"
      class="lm-table-more"
      :disabled="disabled"
      aria-haspopup="dialog"
      @click="openSheet"
    >
      {{ moreText }}
      <template #icon>
        <n-icon><ChevronDown /></n-icon>
      </template>
    </n-button>
  </div>

  <!-- 进出场动画:遮罩与面板各挂一个 Transition —— Vue 只在 Transition 的根节点上量过渡时长,
       共同父层包一层的话根节点没有 transition 属性,类名下一帧就被摘掉,退场动画不会播。 -->
  <Teleport to="body">
    <Transition name="lm-action-sheet-scrim">
      <div
        v-if="sheetShow"
        class="lm-action-sheet__scrim"
        @click="closeSheet"
      />
    </Transition>
    <Transition name="lm-action-sheet-panel">
      <div
        v-if="sheetShow"
        class="lm-action-sheet__panel"
        role="dialog"
        aria-modal="true"
        aria-label="行操作"
      >
        <div class="lm-action-sheet__grip" />
        <div class="lm-action-sheet__hd">
          <h4>操作</h4>
          <p v-if="sheetSubtitle">
            {{ sheetSubtitle }}
          </p>
        </div>
        <div class="lm-action-sheet__list">
          <button
            v-for="action in safeOverflow"
            :key="action.key || action.title"
            type="button"
            class="lm-action-sheet__item"
            :disabled="isDisabled(action) || isLoading(action)"
            @click="runFromSheet(action)"
          >
            {{ action.title }}
          </button>
          <!-- 危险项置底(danger 自带红色文字区分,不加分组标题) -->
          <button
            v-for="action in dangerOverflow"
            :key="action.key || action.title"
            type="button"
            class="lm-action-sheet__item is-danger"
            :disabled="isDisabled(action) || isLoading(action)"
            @click="runFromSheet(action)"
          >
            {{ action.title }}
          </button>
        </div>
        <button
          type="button"
          class="lm-action-sheet__cancel"
          @click="closeSheet"
        >
          取消
        </button>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 1rem = 10px(reset.scss: html font-size 62.5%)。动作面板 teleport 到 body,
 * 但节点仍由本组件渲染,scoped 作用域照常命中。 */

.lm-table-select {
  min-width: 0;
}

/* ── 进出场动画 ───────────────────────────────────────────────────
   遮罩淡入淡出、面板自下而上滑入(iOS sheet 那类先快后缓的曲线);
   退场期间不吃点击,避免连点重复触发。 */
.lm-action-sheet-scrim-enter-active,
.lm-action-sheet-scrim-leave-active {
  transition: opacity 0.2s ease;
}
.lm-action-sheet-scrim-enter-from,
.lm-action-sheet-scrim-leave-to {
  opacity: 0;
}

.lm-action-sheet-panel-enter-active,
.lm-action-sheet-panel-leave-active {
  transition: transform 0.24s cubic-bezier(0.32, 0.72, 0, 1);
}
.lm-action-sheet-panel-enter-from,
.lm-action-sheet-panel-leave-to {
  transform: translateY(100%);
}

.lm-action-sheet-scrim-leave-active,
.lm-action-sheet-panel-leave-active {
  pointer-events: none;
}

/* ── 底部动作面板 ───────────────────────────────────────────────── */
.lm-action-sheet__scrim {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: rgba(15, 18, 25, 0.42);
}

.lm-action-sheet__panel {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 3001;
  max-height: 86vh;
  overflow-y: auto;
  padding: 0.8rem 1.2rem 1.2rem;
  background: var(--c-fill-1);
  border-radius: 1.6rem 1.6rem 0 0;
  box-shadow: 0 -1rem 3.4rem rgba(15, 18, 25, 0.22);
}

/* 原生按钮重置:浏览器默认 buttonface 底 + Arial 字体。必须写在具体规则之前。 */
.lm-action-sheet__item,
.lm-action-sheet__cancel {
  font: inherit;
  background: none;
  cursor: pointer;
}

.lm-action-sheet__grip {
  width: 3.6rem;
  height: 0.4rem;
  margin: 0.4rem auto 1rem;
  border-radius: 0.2rem;
  background: var(--c-line-strong);
}

.lm-action-sheet__hd {
  padding: 0 0.6rem 1rem;
}

.lm-action-sheet__hd h4 {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--c-strong);
}

.lm-action-sheet__hd p {
  margin: 0.3rem 0 0;
  font-family: ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace;
  font-size: 1.2rem;
  font-variant-numeric: tabular-nums;
  color: var(--c-default);
}

.lm-action-sheet__list {
  display: grid;
  gap: 0.2rem;
}

/* 48px 行高、左对齐;悬浮只改中性底色 —— 危险项的文字色不落在红底上 */
.lm-action-sheet__item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 4.8rem;
  padding: 0 1.2rem;
  border-radius: 1rem;
  font-size: 1.5rem;
  color: var(--c-strong);
  text-align: left;
  transition: background 0.15s ease;
}

.lm-action-sheet__item:hover:not(:disabled) {
  background: var(--c-fill-4);
}

.lm-action-sheet__item:focus-visible {
  outline: 2px solid var(--c-primary);
  outline-offset: -2px;
}

.lm-action-sheet__item:disabled {
  color: var(--c-disabled);
  cursor: not-allowed;
}

.lm-action-sheet__item.is-danger {
  color: var(--c-error-text);
}

.lm-action-sheet__cancel {
  width: 100%;
  min-height: 4.4rem;
  margin-top: 1rem;
  border: 1px solid var(--c-line-strong);
  border-radius: 1rem;
  font-size: 1.4rem;
  font-weight: 500;
  color: var(--c-strong);
  background: var(--c-fill-1);
  transition: border-color 0.15s ease;
}

.lm-action-sheet__cancel:hover {
  border-color: var(--c-secondary);
}

.lm-action-sheet__cancel:focus-visible {
  outline: 2px solid var(--c-primary);
  outline-offset: 2px;
}

/* 尊重系统「减弱动态效果」:直接切换,不做位移与淡入 */
@media (prefers-reduced-motion: reduce) {
  .lm-action-sheet-scrim-enter-active,
  .lm-action-sheet-scrim-leave-active,
  .lm-action-sheet-panel-enter-active,
  .lm-action-sheet-panel-leave-active {
    transition: none;
  }
}
</style>
