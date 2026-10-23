<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { NButton, NIcon, NModal, NScrollbar, NTooltip } from 'naive-ui'
import { SearchOutline } from '@vicons/ionicons5'
import type { GlobalSearchItem } from './types'
import type { pinyin } from 'pinyin-pro'

defineOptions({ name: 'LmGlobalSearch' })

interface Props {
  /** 搜索数据源:页面导航 + 快捷操作,由外部按权限过滤后传入 */
  items: GlobalSearchItem[]
  /** 输入框占位文案 */
  placeholder?: string
  /** 是否注册 Ctrl/⌘+K 全局快捷键 */
  hotkey?: boolean
  /** 最近使用记录的 localStorage key;不传则不记录。建议按应用区分,如 llmpod:admin:search-recent */
  recentKey?: string
  /** 触发形态:icon 头部图标按钮(默认) / bar 侧栏搜索框 */
  variant?: 'icon' | 'bar'
  /** bar 形态的折叠态:收起占位文字与快捷键,宽度收成方形图标按钮 */
  collapsed?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  placeholder: '搜索页面或操作',
  hotkey: true,
  recentKey: undefined,
  variant: 'icon',
  collapsed: false,
})

const show = ref(false)
const keyword = ref('')
const activeIndex = ref(0)
const inputRef = ref<HTMLInputElement>()
const listRef = ref<HTMLElement>()

const isMac = /Mac|iPhone|iPad/.test(navigator.platform)

/** 单项搜索索引:label 小写 + 拼音全拼/首字母 + 关键词,按 item 缓存(WeakMap 随数据源回收) */
interface SearchIndex {
  labelLower: string
  pinyinFull: string
  pinyinFirst: string
  keywords: string[]
}
let indexCache = new WeakMap<GlobalSearchItem, SearchIndex>()

/**
 * pinyin-pro(约 300KB)按需加载:仅 Ctrl+K 面板的拼音检索用,不进首屏。
 * 首次 open() 才拉取;未就绪时 getIndex 跳过拼音字段,label/关键词匹配不受影响,
 * 就绪后 pinyinFn 赋值触发 filtered 重算并重建索引,拼音匹配自动生效。
 */
const pinyinFn = shallowRef<typeof pinyin | null>(null)
let pinyinLoading: Promise<typeof pinyin> | null = null

function ensurePinyin(): Promise<typeof pinyin> {
  if (!pinyinLoading) {
    pinyinLoading = import('pinyin-pro').then((mod) => {
      pinyinFn.value = mod.pinyin
      indexCache = new WeakMap()
      return mod.pinyin
    })
  }
  return pinyinLoading
}

function getIndex(item: GlobalSearchItem): SearchIndex {
  let index = indexCache.get(item)
  if (!index) {
    const pinyin = pinyinFn.value
    index = {
      labelLower: item.label.toLowerCase(),
      pinyinFull: pinyin
        ? pinyin(item.label, { toneType: 'none', type: 'array' }).join('').toLowerCase()
        : '',
      pinyinFirst: pinyin
        ? pinyin(item.label, { pattern: 'first', toneType: 'none', type: 'array' })
            .join('')
            .toLowerCase()
        : '',
      keywords: (item.keywords ?? []).map((word) => word.toLowerCase()),
    }
    indexCache.set(item, index)
  }
  return index
}

/** 单 token 匹配打分:越小越靠前,-1 未命中;label 优先,拼音(未加载时跳过)次之,关键词兜底 */
function tokenScore(index: SearchIndex, token: string): number {
  if (index.labelLower.startsWith(token)) return 0
  if (index.labelLower.includes(token)) return 1
  if (index.pinyinFirst && index.pinyinFirst.startsWith(token)) return 2
  if (index.pinyinFull && index.pinyinFull.startsWith(token)) return 3
  if (
    index.pinyinFirst && index.pinyinFirst.includes(token) ||
    index.pinyinFull && index.pinyinFull.includes(token)
  ) return 4
  if (index.keywords.some((word) => word.startsWith(token))) return 5
  if (index.keywords.some((word) => word.includes(token))) return 6
  return -1
}

/** 组名 → 排序权重:按各组在数据源中首次出现的位置,保证搜索结果组不交错 */
const groupOrder = computed(() => {
  const order = new Map<string, number>()
  for (const item of props.items) {
    if (!order.has(item.group)) order.set(item.group, order.size)
  }
  return order
})

/** 最近使用:localStorage 持久化的 key 列表,最新在前,最多 RECENT_MAX 条 */
const RECENT_MAX = 5

function readRecentKeys(): string[] {
  if (!props.recentKey) return []
  try {
    const keys: unknown = JSON.parse(localStorage.getItem(props.recentKey) ?? '[]')
    return Array.isArray(keys) ? keys.filter((key): key is string => typeof key === 'string') : []
  } catch {
    return []
  }
}

const recentKeys = ref<string[]>(readRecentKeys())

function pushRecent(key: string) {
  if (!props.recentKey) return
  recentKeys.value = [key, ...recentKeys.value.filter((item) => item !== key)].slice(0, RECENT_MAX)
  localStorage.setItem(props.recentKey, JSON.stringify(recentKeys.value))
}

/** 过滤:空白展示全部(最近使用置顶为独立组);多 token(空格分隔)要求全部命中,组序稳定、组内按总分排序 */
const filtered = computed(() => {
  const tokens = keyword.value.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!tokens.length) {
    if (!recentKeys.value.length) return props.items
    const recentSet = new Set(recentKeys.value)
    const recentItems = recentKeys.value
      .map((key) => props.items.find((item) => item.key === key))
      .filter((item): item is GlobalSearchItem => item !== undefined)
      .map((item) => ({ ...item, group: '最近使用' }))
    return [...recentItems, ...props.items.filter((item) => !recentSet.has(item.key))]
  }
  return props.items
    .map((item) => {
      const index = getIndex(item)
      let score = 0
      for (const token of tokens) {
        const single = tokenScore(index, token)
        if (single < 0) return null
        score += single
      }
      return { item, score }
    })
    .filter((entry): entry is { item: GlobalSearchItem; score: number } => entry !== null)
    .sort(
      (a, b) =>
        (groupOrder.value.get(a.item.group) ?? 0) - (groupOrder.value.get(b.item.group) ?? 0) ||
        a.score - b.score,
    )
    .map((entry) => entry.item)
})

/** 展示行:filtered 已保证同组连续(组序取首次出现位置,组内按分数),groupStart 标记段首 */
const rows = computed(() =>
  filtered.value.map((item, index) => ({
    item,
    index,
    groupStart: index === 0 || filtered.value[index - 1].group !== item.group,
  })),
)

watch(keyword, () => {
  activeIndex.value = 0
})

function open() {
  show.value = true
  keyword.value = ''
  activeIndex.value = 0
  void ensurePinyin()
  void nextTick(() => inputRef.value?.focus())
}

async function run(item: GlobalSearchItem) {
  show.value = false
  pushRecent(item.key)
  await item.onSelect()
}

function scrollActiveIntoView() {
  void nextTick(() => {
    listRef.value
      ?.querySelector(`[data-index="${activeIndex.value}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  })
}

function onKeydown(event: KeyboardEvent) {
  // 输入法组词中不触发:IME 回车确认候选词/方向键选词不应执行或抢动高亮项
  if (event.isComposing) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const step = event.key === 'ArrowDown' ? 1 : -1
    const total = rows.value.length
    if (total) activeIndex.value = (activeIndex.value + step + total) % total
    scrollActiveIntoView()
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    const target = rows.value[activeIndex.value]
    if (target) void run(target.item)
  }
}

function onGlobalKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    if (show.value) show.value = false
    else open()
  }
}

onMounted(() => {
  if (props.hotkey) window.addEventListener('keydown', onGlobalKeydown)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKeydown))

/** label 高亮:仅原文子串命中可高亮(拼音命中无法映射回字符区间,不高亮) */
function labelParts(label: string): { text: string; hit: boolean }[] {
  const tokens = keyword.value.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const ranges: Array<[number, number]> = []
  for (const token of tokens) {
    const at = label.toLowerCase().indexOf(token)
    if (at >= 0) ranges.push([at, at + token.length])
  }
  if (!ranges.length) return [{ text: label, hit: false }]
  ranges.sort((a, b) => a[0] - b[0])
  const parts: { text: string; hit: boolean }[] = []
  let cursor = 0
  for (const [start, end] of ranges) {
    if (start > cursor) parts.push({ text: label.slice(cursor, start), hit: false })
    if (end > cursor) parts.push({ text: label.slice(Math.max(cursor, start), end), hit: true })
    cursor = Math.max(cursor, end)
  }
  if (cursor < label.length) parts.push({ text: label.slice(cursor), hit: false })
  return parts
}

defineExpose({ open })
</script>

<template>
  <!-- 触发入口 · icon 形态:头部纯图标按钮,点击或 Ctrl/⌘+K 唤起 -->
  <n-tooltip v-if="variant === 'icon'" trigger="hover" :delay="500">
    <template #trigger>
      <n-button quaternary circle :focusable="false" :aria-label="placeholder" @click="open">
        <template #icon>
          <n-icon :size="18">
            <SearchOutline />
          </n-icon>
        </template>
      </n-button>
    </template>
    {{ placeholder }}({{ isMac ? '⌘K' : 'Ctrl K' }})
  </n-tooltip>

  <!-- 触发入口 · bar 形态:侧栏搜索框,折叠态收成方形图标按钮 -->
  <button
    v-else
    type="button"
    class="h-[3.6rem] flex items-center rounded-[var(--n-border-radius)] border border-line bg-fill-2 text-tertiary cursor-pointer transition-colors hover:bg-fill-4 hover:border-line-strong"
    :class="collapsed ? 'w-[3.6rem] justify-center' : 'w-full gap-[.8rem] px-[1rem]'"
    :aria-label="placeholder"
    @click="open"
  >
    <n-icon :size="16" class="shrink-0">
      <SearchOutline />
    </n-icon>
    <template v-if="!collapsed">
      <span class="flex-1 min-w-0 truncate text-left text-sm">{{ placeholder }}</span>
      <span class="lm-global-search-kbd shrink-0">{{ isMac ? '⌘K' : 'Ctrl K' }}</span>
    </template>
  </button>

  <n-modal v-model:show="show" :auto-focus="false" class="lm-global-search">
    <div
      class="w-[58rem] max-w-[92vw] rounded-[1rem] bg-fill-1 border border-line shadow-2xl overflow-hidden"
    >
      <div class="flex items-center gap-[1rem] px-[1.6rem] border-b border-line">
        <n-icon :size="18" class="text-tertiary shrink-0">
          <SearchOutline />
        </n-icon>
        <input
          ref="inputRef"
          v-model="keyword"
          class="flex-1 h-[5.2rem] bg-transparent outline-none text-sm text-strong lm-global-search-input"
          :placeholder="placeholder"
          @keydown="onKeydown"
        />
        <span class="lm-global-search-kbd cursor-pointer" @click="show = false">Esc</span>
      </div>

      <n-scrollbar style="max-height: 40rem">
        <div v-if="rows.length" ref="listRef" class="p-[.8rem]">
          <template v-for="row in rows" :key="row.item.key">
            <div
              v-if="row.groupStart"
              class="px-[.8rem] pt-[.8rem] pb-[.4rem] text-xs text-tertiary first:pt-0"
            >
              {{ row.item.group }}
            </div>
            <div
              :data-index="row.index"
              class="flex items-center gap-[1rem] px-[.8rem] h-[3.6rem] rounded-[.6rem] cursor-pointer text-sm"
              :class="row.index === activeIndex ? 'bg-fill-3 text-strong' : 'text-default'"
              @click="run(row.item)"
              @mouseenter="activeIndex = row.index"
            >
              <component
                :is="row.item.icon"
                v-if="row.item.icon"
                class="w-[1.6rem] h-[1.6rem] shrink-0 text-icon"
              />
              <span class="flex-1 whitespace-nowrap overflow-hidden text-ellipsis">
                <template v-for="(part, partIndex) in labelParts(row.item.label)" :key="partIndex">
                  <span :class="part.hit ? 'text-primary font-bold' : ''">{{ part.text }}</span>
                </template>
              </span>
              <span v-if="row.index === activeIndex" class="lm-global-search-kbd">Enter</span>
              <span v-if="row.item.meta" class="text-xs text-tertiary shrink-0">{{
                row.item.meta
              }}</span>
            </div>
          </template>
        </div>
        <div v-else class="h-[12rem] flex items-center justify-center text-sm text-tertiary">
          未找到匹配的结果
        </div>
      </n-scrollbar>

      <div
        class="flex items-center gap-[1.6rem] px-[1.6rem] h-[3.6rem] border-t border-line text-xs text-tertiary"
      >
        <span class="flex items-center gap-[.4rem]">
          <span class="lm-global-search-kbd">↑</span>
          <span class="lm-global-search-kbd">↓</span>
          选择
        </span>
        <span class="flex items-center gap-[.4rem]">
          <span class="lm-global-search-kbd">Enter</span>
          打开
        </span>
        <span class="flex items-center gap-[.4rem]">
          <span class="lm-global-search-kbd">Esc</span>
          关闭
        </span>
      </div>
    </div>
  </n-modal>
</template>

<style>
/* 输入框占位符:UnoCSS 66 attributify + placeholder variant 会生成 ::placeholder::placeholder
   非法选择器,改用 CSS 变量在样式层实现占位符着色 */
.lm-global-search-input::placeholder {
  color: var(--c-tertiary);
}

/* kbd 按键样式:非 scoped,modal teleport 到 body 后 scoped 选择器失效 */
.lm-global-search-kbd {
  display: inline-flex;
  align-items: center;
  height: 1.8rem;
  padding: 0 0.5rem;
  border: 1px solid var(--c-line-strong);
  border-radius: 0.4rem;
  background: var(--c-fill-2);
  color: var(--c-tertiary);
  font-size: 1.1rem;
  line-height: 1;
  white-space: nowrap;
}

/* 命令面板顶部对齐(默认 flex 居中):跟随主流 Ctrl+K 面板交互 */
.n-modal-container:has(.lm-global-search) {
  align-items: flex-start;
  padding-top: 12vh;
}
</style>
