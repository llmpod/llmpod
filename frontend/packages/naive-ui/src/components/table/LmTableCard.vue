<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { NEmpty, NIcon, NSpin } from 'naive-ui'
import type { DataTableBaseColumn, DataTableColumn, DataTableRowData } from 'naive-ui'
import { Checkmark, ChevronDown } from '@vicons/ionicons5'
import { ACTION_BUDGET, provideLmCardActionBudget } from './action-budget'

defineOptions({ name: 'LmTableCard', inheritAttrs: false })

/** 列定义扩展:操作列由 useOperate 打 __lmOperate 标记。
 * DataTableColumn 是 union(selection/expand/group/base),只能交叉不能 extends */
type CardColumn = DataTableColumn & { __lmOperate?: boolean }

/** 数据列(卡片消费形状):__lmOperate 随列透传 */
type CardDataColumn = DataTableBaseColumn & { key: string; __lmOperate?: boolean }

/** 卡面默认可见字段数:一屏可读上限,超出部分收进「展开 N 项」 */
const VISIBLE_FIELD_LIMIT = 3

interface Props {
  /** 表格数据(当前页切片,与表格一致) */
  rows: DataTableRowData[]
  /** 生效列定义(含 selection/operate 列,由 useTable 传入) */
  columns: CardColumn[]
  /** 行 key 提取函数 */
  rowKey: (row: DataTableRowData) => string | number
  /** 受控选中行键(与表格复选列同一状态源) */
  checkedRowKeys?: Array<string | number>
  /** 加载态 */
  loading?: boolean
  /** 卡片主标题列 key;缺省第一个数据列 */
  titleKey?: string
  /** 卡面可见字段列 key 白名单(建议 2–3 项);缺省取前 VISIBLE_FIELD_LIMIT 个数据列。
   * 契约:undefined = 自动取前 N 个;判定必须用 == null,防止未来误加空数组默认值产生二义性 */
  fieldKeys?: string[]
  /** 强调列 key(如金额):提到卡首行右端,17px 等宽数字;缺省不渲染强调位 */
  emphasisKey?: string
  /** 标签列 key:卡首行渲染为中性灰标签(语义交给强调数字与边轨);缺省不渲染标签 */
  tagKey?: string
  /** 异常行判定:命中时卡片左侧出现 3px 边轨。
   * true 等价 'error';缺省无边轨(正常行不涂色,异常靠"多出来的东西"被看见) */
  alertWhen?: (row: DataTableRowData) => boolean | 'warning' | 'error'
  /** 卡片密度:card 记录卡(默认)/ compact 56px 紧凑行 */
  density?: 'card' | 'compact'
}

const props = withDefaults(defineProps<Props>(), {
  checkedRowKeys: () => [],
  loading: false,
  titleKey: '',
  fieldKeys: undefined,
  emphasisKey: '',
  tagKey: '',
  alertWhen: undefined,
  density: 'card',
})

const emit = defineEmits<{
  /** 勾选/取消一行(键集合由父层维护,保证与表格选择语义一致) */
  toggleCheck: [row: DataTableRowData, checked: boolean]
}>()

/** 复选列(selection 插件);取列对象以读取行级 disabled 配置 */
const selectionCol = computed(
  () => props.columns.find((col) => col.type === 'selection') as CardColumn | undefined,
)

/** selection 列行级禁用(deleting 等状态行不可勾选,与表格复选列语义一致) */
function isRowDisabled(row: DataTableRowData): boolean {
  const disabled = (selectionCol.value as { disabled?: boolean | ((row: DataTableRowData) => boolean) } | undefined)?.disabled
  if (typeof disabled === 'function') return disabled(row)
  return disabled === true
}

/** 操作列(useOperate 插件,render 内渲染 LmTableSelect) */
const operateCol = computed(
  () => props.columns.find((col) => col.__lmOperate === true) as CardDataColumn | undefined,
)

/** 数据列:排除 selection/expand/操作列与无 key 列(!col.type 已把 union 收窄到含 key 的成员) */
const dataColumns = computed(() =>
  props.columns.filter(
    (col) => !col.type && col.__lmOperate !== true && col.key != null,
  ) as CardDataColumn[],
)

function colByKey(key: string): CardDataColumn | undefined {
  if (!key) return undefined
  return dataColumns.value.find((col) => col.key === key)
}

/** 主字段列:显式 titleKey 命中优先,否则第一个数据列 */
const titleCol = computed(() => colByKey(props.titleKey) ?? dataColumns.value[0])

/** 标签列 / 强调列:未配置时不参与卡面,字段落回定义列表 */
const tagCol = computed(() => colByKey(props.tagKey))
const emphasisCol = computed(() => colByKey(props.emphasisKey))

/** 定义列表候选列:排除已占用卡首/主标题的列 */
const kvCandidates = computed(() =>
  dataColumns.value.filter(
    (col) => col !== titleCol.value && col !== tagCol.value && col !== emphasisCol.value,
  ),
)

/** 卡面可见字段:白名单优先(保列定义顺序),否则取前 VISIBLE_FIELD_LIMIT 个 */
const fieldCols = computed(() =>
  props.fieldKeys == null
    ? kvCandidates.value.slice(0, VISIBLE_FIELD_LIMIT)
    : kvCandidates.value.filter((col) => props.fieldKeys!.includes(col.key)),
)

/** 折叠字段:白名单之外的候选列;无折叠项时不出「展开 0 项」入口 */
const hiddenCols = computed(() =>
  props.fieldKeys == null
    ? kvCandidates.value.slice(VISIBLE_FIELD_LIMIT)
    : kvCandidates.value.filter((col) => !props.fieldKeys!.includes(col.key)),
)

/** 紧凑视图次行字段:取可见字段前 2 项,单行「A · B」扫读 */
const compactSubCols = computed(() => fieldCols.value.slice(0, 2))

/** 展开明细的行键集合(按 rowKey 记录,数据刷新后自然失效) */
const expandedKeys = ref(new Set<string | number>())

function isExpanded(row: DataTableRowData): boolean {
  return expandedKeys.value.has(props.rowKey(row))
}

function toggleExpand(row: DataTableRowData): void {
  const key = props.rowKey(row)
  if (expandedKeys.value.has(key)) expandedKeys.value.delete(key)
  else expandedKeys.value.add(key)
}

/** 异常边轨色:true 等价 error;缺省无边轨 */
function alertClass(row: DataTableRowData): string {
  const tone = props.alertWhen?.(row)
  if (!tone) return ''
  return tone === 'warning' ? 'is-alert-warning' : 'is-alert-error'
}

/** 列值渲染:render 优先(开关/标签等自定义渲染原样生效),否则取原始值;空值占位 — */
function renderValue(col: CardDataColumn, row: DataTableRowData, index: number) {
  if (col.render) return col.render(row, index)
  const value = row[col.key]
  return value == null || value === '' ? '—' : String(value)
}

function isChecked(row: DataTableRowData): boolean {
  return props.checkedRowKeys.includes(props.rowKey(row))
}

function onCheck(row: DataTableRowData, checked: boolean): void {
  emit('toggleCheck', row, checked)
}

/* ── 行操作内联预算 ───────────────────────────────────────────────
   卡片把自己的实测宽度交给 LmTableSelect,由它按预算自动切片:
   操作项多时只内联前 N 项,其余沉进「更多」(窄屏为底部动作面板)。 */

const rootRef = ref<HTMLElement | null>(null)
/** 卡片实测宽度;0 = 尚未测量(首帧),消费方回退到内联上限 */
const cardWidth = ref(0)
/** 「展开 N 项」入口是否出现 —— 它占位会挤压操作区,需计入预算 */
const hasToggle = computed(() => hiddenCols.value.length > 0)

/** 行标识纯文本:取主标题列的原始值(自定义 render 无法取文本时留空) */
function rowLabel(row: unknown): string {
  const col = titleCol.value
  if (!col || !row) return ''
  const raw = (row as DataTableRowData)[col.key]
  return raw == null || raw === '' ? '' : String(raw)
}

provideLmCardActionBudget({
  cardWidth,
  hasToggle,
  // 预算与渲染取同一个间距:默认的 12px 按钮间距会让 5 项内联更早溢出
  actionGap: ACTION_BUDGET.gap,
  rowLabel,
})

let widthObserver: ResizeObserver | null = null

function measureCard() {
  cardWidth.value = rootRef.value?.clientWidth ?? 0
}

onMounted(() => {
  measureCard()
  if (typeof ResizeObserver === 'undefined' || !rootRef.value) return
  widthObserver = new ResizeObserver(measureCard)
  widthObserver.observe(rootRef.value)
})

onBeforeUnmount(() => {
  widthObserver?.disconnect()
  widthObserver = null
})

/** VNode 桥:模板中透出列 render 函数产出的 VNode(VNodeChild);type: null = 任意类型透传 */
const VNodeBridge = defineComponent({
  name: 'LmTableCardVNodeBridge',
  props: {
    vnode: { type: null, default: '' },
    /** 宿主标签:卡首/行内用 span,定义列表用 div(默认) */
    tag: { type: String, default: 'div' },
  },
  setup: (p) => () => h(p.tag, { class: 'min-w-0' }, p.vnode as never),
})
</script>

<template>
  <!-- inheritAttrs: false + 显式 v-bind:attrs 落在根节点,避免透传到内部 n-spin -->
  <div
    ref="rootRef"
    v-bind="$attrs"
    class="lm-table-card"
  >
    <n-spin :show="loading">
      <!-- 空态:沿用页面 #empty 插槽(LmTableEmpty 等),无插槽时兜底 n-empty -->
      <div
        v-if="rows.length === 0"
        class="py-[1.6rem]"
      >
        <slot name="empty">
          <n-empty description="暂无数据" />
        </slot>
      </div>

      <!-- 紧凑视图:两行一条,56px 高,适合快速扫读 -->
      <div
        v-else-if="density === 'compact'"
        class="lm-table-card__list"
      >
        <article
          v-for="(row, index) in rows"
          :key="rowKey(row)"
          class="lm-table-card__row"
          :class="alertClass(row)"
        >
          <div class="lm-table-card__rowmid">
            <div class="lm-table-card__rowl1">
              <span class="lm-table-card__rowname">
                <VNodeBridge
                  v-if="titleCol"
                  tag="span"
                  :vnode="renderValue(titleCol, row, index)"
                />
              </span>
              <span
                v-if="tagCol"
                class="lm-table-card__tag"
              >
                <VNodeBridge
                  tag="span"
                  :vnode="renderValue(tagCol, row, index)"
                />
              </span>
            </div>
            <div
              v-if="compactSubCols.length"
              class="lm-table-card__rowl2"
            >
              <template
                v-for="(col, subIndex) in compactSubCols"
                :key="col.key"
              >
                <span
                  v-if="subIndex > 0"
                  class="lm-table-card__sep"
                >·</span>
                <VNodeBridge
                  tag="span"
                  :vnode="renderValue(col, row, index)"
                />
              </template>
            </div>
          </div>
          <span
            v-if="emphasisCol"
            class="lm-table-card__rowamt"
          >
            <VNodeBridge
              tag="span"
              :vnode="renderValue(emphasisCol, row, index)"
            />
          </span>
        </article>
      </div>

      <!-- 记录卡:卡首(标签 + 强调数字)→ 主标题 → 定义列表 → 展开区 → 页脚操作 -->
      <div
        v-else
        class="lm-table-card__list"
      >
        <article
          v-for="(row, index) in rows"
          :key="rowKey(row)"
          class="lm-table-card__item"
          :class="[alertClass(row), { 'is-sel': isChecked(row), 'is-batch': !!selectionCol }]"
        >
          <!-- 勾选命中区:44×44,固定在卡片右上角,不参与内容排版 -->
          <button
            v-if="selectionCol"
            type="button"
            class="lm-table-card__pick"
            role="checkbox"
            :aria-checked="isChecked(row)"
            :aria-label="isChecked(row) ? '取消选择该行' : '选择该行'"
            :disabled="isRowDisabled(row)"
            @click="onCheck(row, !isChecked(row))"
          >
            <span class="lm-table-card__box"><n-icon><Checkmark /></n-icon></span>
          </button>

          <div class="lm-table-card__main">
            <!-- 卡首:标签中性灰 + 强调数字(金额)等宽对右 -->
            <div
              v-if="tagCol || emphasisCol"
              class="lm-table-card__top"
            >
              <span
                v-if="tagCol"
                class="lm-table-card__tag"
              >
                <VNodeBridge
                  tag="span"
                  :vnode="renderValue(tagCol, row, index)"
                />
              </span>
              <span
                v-if="emphasisCol"
                class="lm-table-card__amount"
              >
                <VNodeBridge
                  tag="span"
                  :vnode="renderValue(emphasisCol, row, index)"
                />
              </span>
            </div>

            <!-- 主题行:主标题允许两行,截断不再发生在模型名的区分位上 -->
            <div class="lm-table-card__sub">
              <span class="lm-table-card__subject">
                <VNodeBridge
                  v-if="titleCol"
                  tag="span"
                  :vnode="renderValue(titleCol, row, index)"
                />
              </span>
            </div>

            <!-- 定义列表:label 定宽 68px,值同起点,跨卡形成可比较的列 -->
            <dl
              v-if="fieldCols.length"
              class="lm-table-card__kv"
            >
              <div
                v-for="col in fieldCols"
                :key="col.key"
                class="lm-table-card__kvrow"
              >
                <dt class="lm-table-card__label">
                  {{ col.title }}
                </dt>
                <dd
                  class="lm-table-card__value"
                  :class="{ 'is-right': col.align === 'right' }"
                >
                  <VNodeBridge :vnode="renderValue(col, row, index)" />
                </dd>
              </div>
            </dl>

            <!-- 折叠明细:其余字段,无折叠项时不渲染 -->
            <div
              v-if="hiddenCols.length && isExpanded(row)"
              class="lm-table-card__hidden"
            >
              <dl class="lm-table-card__kv is-flush">
                <div
                  v-for="col in hiddenCols"
                  :key="col.key"
                  class="lm-table-card__kvrow"
                >
                  <dt class="lm-table-card__label">
                    {{ col.title }}
                  </dt>
                  <dd
                    class="lm-table-card__value"
                    :class="{ 'is-right': col.align === 'right' }"
                  >
                    <VNodeBridge :vnode="renderValue(col, row, index)" />
                  </dd>
                </div>
              </dl>
            </div>
          </div>

          <!-- 页脚操作行:展开入口 + 行操作(命中区 ≥44px) -->
          <div
            v-if="hiddenCols.length || operateCol"
            class="lm-table-card__foot"
          >
            <button
              v-if="hiddenCols.length"
              type="button"
              class="lm-table-card__toggle"
              :aria-expanded="isExpanded(row)"
              @click="toggleExpand(row)"
            >
              {{ isExpanded(row) ? '收起明细' : `展开 ${hiddenCols.length} 项` }}
              <n-icon class="lm-table-card__caret">
                <ChevronDown />
              </n-icon>
            </button>
            <span
              v-if="operateCol"
              class="lm-table-card__extra"
            >
              <VNodeBridge :vnode="operateCol.render && operateCol.render(row, index)" />
            </span>
          </div>
        </article>
      </div>
    </n-spin>
  </div>
</template>

<style scoped>
/* 窄屏卡片形态。token 取自 packages/naive-ui/src/theme/tokens.scss(1rem = 10px)。
 * 三条硬约束:可点命中区 ≥44px、正文对比 ≥4.5:1、标签定宽保证值同起点。 */

.lm-table-card__list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

/* 原生按钮重置:浏览器默认给 button 上 buttonface 底 + Arial 字体,与卡面其余文字不一致。
 * 必须写在下方具体规则之前,否则 font 简写会覆盖字号。 */
.lm-table-card__toggle,
.lm-table-card__pick {
  font: inherit;
  color: inherit;
  cursor: pointer;
  background: none;
}

/* ─── 记录卡 ─────────────────────────────────────────────────────── */
.lm-table-card__item {
  position: relative;
  overflow: hidden;
  background: var(--c-fill-1);
  /* 原 border-line #f2f2f2 与白底仅 1.11:1,20 张卡会连成一片 */
  border: 1px solid var(--c-line-strong);
  border-radius: 1.2rem;
  transition: border-color 0.15s ease, background 0.15s ease;
}

/* 选中态:主色浅底 + 主色描边,跟随 theme-lab 改色 */
.lm-table-card__item.is-sel {
  background: var(--c-primary-tint);
  border-color: var(--c-primary-edge);
}

/* 异常边轨:仅异常项出现,正常行不涂色 */
.lm-table-card__item.is-alert-warning::before,
.lm-table-card__item.is-alert-error::before,
.lm-table-card__row.is-alert-warning::before,
.lm-table-card__row.is-alert-error::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 0.3rem;
}
.lm-table-card__item.is-alert-warning::before,
.lm-table-card__row.is-alert-warning::before {
  background: var(--c-warning-mark);
}
.lm-table-card__item.is-alert-error::before,
.lm-table-card__row.is-alert-error::before {
  background: var(--c-error-text);
}

.lm-table-card__main {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  padding: 1.2rem 1.4rem 1rem 1.5rem;
}
/* 勾选框占右上角 44×44:内容让位,避免强调数字被压 */
.lm-table-card__item.is-batch .lm-table-card__main {
  padding-right: 5.6rem;
}

/* ─── 卡首:标签 + 强调数字 ───────────────────────────────────────── */
.lm-table-card__top {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  min-width: 0;
}

/* 标签刻意保持中性:语义交给强调数字与边轨,避免多个彩底标签互相争夺注意力 */
.lm-table-card__tag {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  height: 2rem;
  padding: 0 0.7rem;
  border-radius: 0.4rem;
  background: var(--c-fill-4);
  color: var(--c-default);
  font-size: 1.1rem;
  line-height: 1;
  font-weight: 500;
  letter-spacing: 0.02em;
}
/* 列 render 产出的 NTag 在卡内统一中性化(仅窄屏卡片,桌面表格不受影响) */
.lm-table-card__tag :deep(.n-tag) {
  height: 2rem;
  padding: 0 0.7rem;
  border-radius: 0.4rem;
  background: var(--c-fill-4);
  color: var(--c-default);
  border: 0;
  font-size: 1.1rem;
}

/* 强调数字:卡片第一落点,等宽 + 右对齐,跨行可比较 */
.lm-table-card__amount {
  margin-left: auto;
  flex: 0 0 auto;
  font-family: ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-size: 1.7rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: nowrap;
  color: var(--c-strong);
}

/* 语义色在卡内改走"文字安全"变体(原值作文字色仅 2.62–3.23:1) */
.lm-table-card :deep(.text-success) {
  color: var(--c-success-text);
}
.lm-table-card :deep(.text-error) {
  color: var(--c-error-text);
}
.lm-table-card :deep(.text-warning) {
  color: var(--c-warning-text);
}
.lm-table-card :deep(.text-info) {
  color: var(--c-info-text);
}

/* ─── 主题行 ─────────────────────────────────────────────────────── */
.lm-table-card__sub {
  display: flex;
  align-items: flex-start;
  gap: 0.8rem;
  min-width: 0;
}
.lm-table-card__subject {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 1.5rem;
  font-weight: 600;
  line-height: 1.35;
  color: var(--c-strong);
  /* 原 truncate 单行省略会截掉区分位,改为换行 + 两行封顶 */
  overflow-wrap: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ─── 定义列表:label 定宽 → 值同起点 ─────────────────────────────── */
.lm-table-card__kv {
  display: grid;
  gap: 0.5rem;
  margin: 0;
  padding: 0.9rem 0 0;
  border-top: 1px solid var(--c-line);
}
.lm-table-card__kv.is-flush {
  padding-top: 0.2rem;
  border-top: 0;
}
.lm-table-card__kvrow {
  display: grid;
  /* 定宽标签是消除"每张卡值起点都不同"的唯一办法 */
  grid-template-columns: 6.8rem minmax(0, 1fr);
  gap: 1rem;
  align-items: baseline;
}
.lm-table-card__label {
  /* 原 text-tertiary #c7c7c7 = 1.69:1,远低于正文 4.5:1 */
  color: var(--c-default);
  font-size: 1.2rem;
  white-space: nowrap;
}
.lm-table-card__value {
  margin: 0;
  min-width: 0;
  color: var(--c-strong);
  font-size: 1.3rem;
  font-variant-numeric: tabular-nums;
  /* 原 break-all 让中文与请求 ID 从任意字符断行 */
  overflow-wrap: break-word;
}
.lm-table-card__value.is-right {
  text-align: right;
}

/* ─── 页脚操作行 ─────────────────────────────────────────────────── */
/* 左侧「展开」不可压缩,右侧内联操作有硬预算(见 action-budget.ts)。
 * 不换行、不溢出的保证来自按实测卡宽算出的切片,这里的 max-width 只是
 * 配置越界(如超长标签)时的兜底 —— 截断而不是顶出卡片。 */
.lm-table-card__foot {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 0.4rem;
  padding: 0 0.8rem 0.4rem 1.2rem;
  border-top: 1px solid var(--c-line);
}
.lm-table-card__toggle {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 0.6rem;
  min-height: 4.4rem;
  padding: 0 0.8rem;
  font-size: 1.3rem;
  font-weight: 500;
  /* 主色作文字色:暗色下平台主色仅 2.3:1,走提亮后的文字安全变体 */
  color: var(--c-primary-text);
}
.lm-table-card__caret {
  transition: transform 0.18s ease;
}
.lm-table-card__toggle[aria-expanded='true'] .lm-table-card__caret {
  transform: rotate(180deg);
}
.lm-table-card__extra {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  flex: 0 1 auto;
  min-width: 0;
  /* 原 LmTableSelect 只有 28px 文字按钮,窄屏命中区不足 */
  min-height: 4.4rem;
}
.lm-table-card__extra :deep(.n-button) {
  max-width: 6.5em;
  min-height: 4.4rem;
  padding: 0 0.8rem;
  font-size: 1.3rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lm-table-card__extra :deep(.n-button__content) {
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 「更多」:溢出入口,不参与截断 */
.lm-table-card__extra :deep(.lm-table-more) {
  max-width: none;
  overflow: visible;
}
/* 行内主操作(LmTableSelect 的 type="primary")同上;danger 按钮走 error 色,不受影响 */
.lm-table-card__extra :deep(.n-button--primary-type) {
  color: var(--c-primary-text);
}
/* 危险操作作文字色:原 --c-error 白底仅 3.14:1,卡内改走文字安全变体 */
.lm-table-card__extra :deep(.n-button--error-type) {
  color: var(--c-error-text);
}

/* ─── 勾选命中区 ─────────────────────────────────────────────────── */
.lm-table-card__pick {
  position: absolute;
  right: 0;
  top: 0;
  width: 4.4rem;
  height: 4.4rem;
  display: grid;
  place-items: center;
  border-radius: 0 1.2rem 0 1rem;
}
.lm-table-card__box {
  width: 2rem;
  height: 2rem;
  display: grid;
  place-items: center;
  border: 1.5px solid var(--c-secondary);
  border-radius: 0.6rem;
  background: var(--c-fill-1);
  transition: background 0.15s ease, border-color 0.15s ease;
}
.lm-table-card__box :deep(.n-icon) {
  opacity: 0;
  font-size: 1.2rem;
  color: var(--c-inverse);
  transition: opacity 0.12s ease;
}
.lm-table-card__item.is-sel .lm-table-card__box {
  background: var(--c-primary);
  border-color: var(--c-primary);
}
.lm-table-card__item.is-sel .lm-table-card__box :deep(.n-icon) {
  opacity: 1;
  color: #fff;
}
.lm-table-card__pick:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* ─── 紧凑行 ─────────────────────────────────────────────────────── */
.lm-table-card__row {
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  gap: 1rem;
  min-height: 5.6rem;
  padding: 0.8rem 1.2rem 0.8rem 1.4rem;
  background: var(--c-fill-1);
  border: 1px solid var(--c-line-strong);
  border-radius: 1rem;
}
.lm-table-card__row.is-alert-warning::before,
.lm-table-card__row.is-alert-error::before {
  border-radius: 1rem 0 0 1rem;
}
.lm-table-card__rowmid {
  flex: 1;
  min-width: 0;
}
.lm-table-card__rowl1 {
  display: flex;
  align-items: baseline;
  gap: 0.8rem;
  min-width: 0;
}
.lm-table-card__rowname {
  min-width: 0;
  font-size: 1.4rem;
  font-weight: 600;
  color: var(--c-strong);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lm-table-card__rowl2 {
  display: flex;
  align-items: baseline;
  gap: 0.8rem;
  margin-top: 0.2rem;
  font-size: 1.1rem;
  color: var(--c-default);
  font-variant-numeric: tabular-nums;
}
.lm-table-card__sep {
  color: var(--c-secondary);
}
.lm-table-card__rowamt {
  flex: 0 0 auto;
  font-family: ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-size: 1.4rem;
  font-weight: 600;
  white-space: nowrap;
  color: var(--c-strong);
}

/* ─── 焦点可见性(键盘可达) ──────────────────────────────────────── */
.lm-table-card__pick:focus-visible,
.lm-table-card__toggle:focus-visible,
.lm-table-card__extra :deep(.n-button):focus-visible {
  outline: 2px solid var(--c-primary);
  outline-offset: -2px;
  border-radius: 0.6rem;
}

@media (prefers-reduced-motion: reduce) {
  .lm-table-card__item,
  .lm-table-card__caret,
  .lm-table-card__box,
  .lm-table-card__box :deep(.n-icon) {
    transition: none;
  }
}
</style>
