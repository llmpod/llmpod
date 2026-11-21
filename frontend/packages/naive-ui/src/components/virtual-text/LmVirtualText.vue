<script setup lang="ts">
import { CheckmarkOutline, CopyOutline } from '@vicons/ionicons5'
import { NButton, NIcon, NVirtualList } from 'naive-ui'
import { computed, markRaw, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { copyToClipboard } from '../../utils/clipboard'
import {
  DEFAULT_MAX_LINE_CHARS,
  DEFAULT_MAX_VIRTUAL_LINES,
  DEFAULT_PRETTY_LIMIT,
  estimateVirtualTextHeight,
  formatTextSize,
  prepareVirtualText,
  type VirtualTextLine,
} from './format'

export interface LmVirtualTextProps {
  /** 原始数据（支持对象、字符串、{ _raw }、null/undefined） */
  content: unknown
  /** 可选区块标题（展示于顶部工具栏左侧） */
  title?: string
  /** 空值占位文案 */
  emptyText?: string
  /** 虚拟列表最大高度（px，默认 240 对应 24rem） */
  maxHeight?: number
  /** 单行最大字符数（超过物理切分为多条虚拟行，默认 240） */
  maxLineChars?: number
  /** 字符串 JSON 自动美化上限（字符数，默认 512KB） */
  prettyLimit?: number
  /** 虚拟行安全上限（默认 200_000） */
  maxVirtualLines?: number
  /** 是否延迟到下一帧解析内容（先让弹窗与基础信息完成首帧绘制），默认 true */
  defer?: boolean
  /** 是否展示顶部工具栏（元信息与复制按钮），默认 true */
  showToolbar?: boolean
}

const props = withDefaults(defineProps<LmVirtualTextProps>(), {
  title: '',
  emptyText: '—',
  maxHeight: 240,
  maxLineChars: DEFAULT_MAX_LINE_CHARS,
  prettyLimit: DEFAULT_PRETTY_LIMIT,
  maxVirtualLines: DEFAULT_MAX_VIRTUAL_LINES,
  defer: true,
  showToolbar: true,
})

const emit = defineEmits<{
  copied: []
  'copy-error': [error: unknown]
}>()

const ITEM_MIN_HEIGHT = 20
const LIST_PADDING_Y = 8

/** 虚拟行列表：shallowRef + markRaw 彻底规避数万行被 Vue 递归代理 */
const lines = shallowRef<VirtualTextLine[]>([])
const empty = ref(true)
const parsing = ref(false)
const skippedPretty = ref(false)
const truncated = ref(false)
const charCount = ref(0)
const logicalLineCount = ref(0)
const copyState = ref<'idle' | 'copied' | 'error'>('idle')

/** 完整原文仅驻留非响应式闭包变量，供一键复制使用，卸载时释放 */
let fullText = ''
let parseTaskSeq = 0
let scheduledHandle: number | ReturnType<typeof setTimeout> | null = null
let scheduledViaRaf = false
let copyResetTimer: ReturnType<typeof setTimeout> | null = null

function cancelScheduledParse(): void {
  if (scheduledHandle === null) return
  if (scheduledViaRaf && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(scheduledHandle as number)
  } else {
    clearTimeout(scheduledHandle as ReturnType<typeof setTimeout>)
  }
  scheduledHandle = null
  scheduledViaRaf = false
}

function applyContent(rawContent: unknown): void {
  const prepared = prepareVirtualText(rawContent, {
    maxLineChars: props.maxLineChars,
    prettyLimit: props.prettyLimit,
    maxVirtualLines: props.maxVirtualLines,
  })
  fullText = prepared.text
  empty.value = prepared.empty
  skippedPretty.value = prepared.skippedPretty
  truncated.value = prepared.truncated
  charCount.value = prepared.charCount
  logicalLineCount.value = prepared.logicalLineCount
  lines.value = markRaw(prepared.lines)
  parsing.value = false
}

function scheduleParse(rawContent: unknown): void {
  cancelScheduledParse()
  parseTaskSeq += 1
  const currentSeq = parseTaskSeq
  copyState.value = 'idle'

  if (rawContent == null) {
    fullText = ''
    empty.value = true
    skippedPretty.value = false
    truncated.value = false
    charCount.value = 0
    logicalLineCount.value = 0
    lines.value = []
    parsing.value = false
    return
  }

  // 短原始值/短字符串直接同步处理，避免无谓闪屏；对象与长字符串走分帧，避免阻断首帧绘制
  const isSmallPrimitive =
    typeof rawContent !== 'object' &&
    (typeof rawContent !== 'string' || rawContent.length <= 4096)

  if (!props.defer || isSmallPrimitive) {
    applyContent(rawContent)
    return
  }

  parsing.value = true
  empty.value = false
  lines.value = []

  const run = () => {
    scheduledHandle = null
    scheduledViaRaf = false
    if (currentSeq !== parseTaskSeq) return
    applyContent(rawContent)
  }

  if (typeof requestAnimationFrame === 'function') {
    scheduledViaRaf = true
    scheduledHandle = requestAnimationFrame(run)
  } else {
    scheduledViaRaf = false
    scheduledHandle = setTimeout(run, 0)
  }
}

watch(
  () => [props.content, props.maxLineChars, props.prettyLimit, props.maxVirtualLines] as const,
  ([nextContent]) => {
    scheduleParse(nextContent)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  parseTaskSeq += 1
  cancelScheduledParse()
  if (copyResetTimer !== null) {
    clearTimeout(copyResetTimer)
    copyResetTimer = null
  }
  fullText = ''
  lines.value = []
})

/** 容器明确高度（px）：短内容紧凑展示，长内容封顶 maxHeight 启用虚拟滚动 */
const viewerHeightPx = computed(() =>
  estimateVirtualTextHeight(lines.value, {
    lineHeight: ITEM_MIN_HEIGHT,
    paddingY: LIST_PADDING_Y,
    maxHeight: props.maxHeight,
  }),
)

/** 行号列宽度：随最大行号位数自适应，等宽对齐不抖动 */
const gutterWidth = computed(() => {
  const digits = Math.max(2, String(Math.max(1, logicalLineCount.value)).length)
  return `${digits + 2}ch`
})

const metaSummary = computed(() => {
  if (empty.value || parsing.value) return ''
  const parts = [`${logicalLineCount.value.toLocaleString('zh-CN')} 行`, formatTextSize(charCount.value)]
  if (skippedPretty.value) parts.push('原样展示')
  if (truncated.value) parts.push('视图已截断')
  return parts.join(' · ')
})

const showHeader = computed(() => Boolean(props.title) || props.showToolbar)

async function handleCopy(): Promise<void> {
  if (empty.value || parsing.value) return
  if (copyResetTimer !== null) {
    clearTimeout(copyResetTimer)
    copyResetTimer = null
  }
  try {
    await copyToClipboard(fullText)
    copyState.value = 'copied'
    emit('copied')
  } catch (error) {
    copyState.value = 'error'
    emit('copy-error', error)
  }
  copyResetTimer = setTimeout(() => {
    copyState.value = 'idle'
    copyResetTimer = null
  }, 1500)
}
</script>

<template>
  <div class="flex flex-col">
    <div
      v-if="showHeader"
      class="flex items-center justify-between gap-[.8rem] mb-[.6rem] min-h-[2.4rem]"
    >
      <div class="flex items-center gap-[.8rem] min-w-0">
        <span
          v-if="title"
          class="text-small font-semibold text-strong shrink-0"
        >
          {{ title }}
        </span>
        <span
          v-if="showToolbar && metaSummary"
          class="text-xs text-secondary truncate"
        >
          {{ metaSummary }}
        </span>
      </div>

      <n-button
        v-if="showToolbar && !empty && !parsing"
        size="tiny"
        quaternary
        @click="handleCopy"
      >
        <template #icon>
          <n-icon>
            <CheckmarkOutline v-if="copyState === 'copied'" />
            <CopyOutline v-else />
          </n-icon>
        </template>
        {{ copyState === 'copied' ? '已复制' : copyState === 'error' ? '复制失败' : '复制' }}
      </n-button>
    </div>

    <!-- 分帧解析占位：避免大 body 阻塞弹窗首帧与基础信息展示 -->
    <div
      v-if="parsing"
      class="rounded-md bg-fill-2 px-[1rem] py-[1rem] text-xs text-secondary"
    >
      内容解析中…
    </div>

    <!-- 空值占位 -->
    <div
      v-else-if="empty"
      class="rounded-md bg-fill-2 px-[1rem] py-[1rem] font-mono text-xs leading-[2rem] text-secondary"
    >
      {{ emptyText }}
    </div>

    <!-- 虚拟滚动主体：背景与圆角放在外层容器，NVirtualList 自身不带 border 且 padding 为数字 px -->
    <div
      v-else
      class="rounded-md bg-fill-2 overflow-hidden"
    >
      <n-virtual-list
        :items="lines"
        :item-size="ITEM_MIN_HEIGHT"
        item-resizable
        key-field="key"
        :padding-top="LIST_PADDING_Y"
        :padding-bottom="LIST_PADDING_Y"
        :style="{ height: `${viewerHeightPx / 10}rem` }"
      >
        <template #default="{ item }">
          <div class="flex items-start px-[1rem] font-mono text-xs leading-[2rem] min-h-[2rem] text-default">
            <span
              class="shrink-0 select-none text-right pr-[1rem] text-tertiary tabular-nums"
              :style="{ width: gutterWidth }"
            >{{ item.lineNo ?? '↳' }}</span>
            <span class="min-w-0 flex-1 whitespace-pre-wrap break-all">{{ item.text || ' ' }}</span>
          </div>
        </template>
      </n-virtual-list>
    </div>
  </div>
</template>
