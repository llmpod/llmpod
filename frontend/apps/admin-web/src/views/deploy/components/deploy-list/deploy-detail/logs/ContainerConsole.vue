<script setup lang="ts">
import { ArrowDownOutline, PlayOutline, StopOutline } from '@vicons/ionicons5'
import { AnsiUp } from 'ansi_up'
import type { VirtualListInst } from 'naive-ui'
import type { DeploymentInstance } from '@/api/deployment'
import { tailContainerLog } from '@/api/tail-log'
import { containerNameFor } from './logUtils'

/**
 * 容器 stdout(POST /orchestration/nodes/{nid}/tasks/tail-log,文档 §4),以弹窗形式打开:
 * NDJSON 常连接按需开,全局单流(开新/切换先 abort,弹窗关闭随组件卸载 abort);
 * 窗口模型——向上翻历史靠重发更大 lines 重开 + 按行内容去重前插;
 * 贴底自动跟随;用户上滚/按住拖动解除跟随并挂角标,贴底自动恢复跟随;断线/结束给重连。
 *
 * 性能:高频 stdout 下逐行 [...lines] 追加是 O(n²) 复制 + 逐行 nextTick 读 scrollHeight 强制布局,
 * 改为行缓冲 + rAF 合帧——帧内到达的行先攒普通数组,下一动画帧一次 concat 替换 + 一次滚动,
 * 每秒百行也只有 ~60 次数组替换;渲染走 n-virtual-list 只保留视口内 DOM。
 */
const props = defineProps<{
  deploymentId: string
  instances: DeploymentInstance[]
  /** 入口实例(实例卡片「容器日志」):打开即定位追踪;不传选首个已调度实例 */
  initialInstanceId?: string
}>()

const ansi = new AnsiUp()

/** 实例选项:有 assigned_node 才可 tail(待调度实例无容器) */
const instanceOptions = computed(() =>
  props.instances.map((inst) => ({
    label: `#${inst.rank} ${inst.role}${
      inst.assigned_node ? ` · ${inst.assigned_node.slice(0, 8)}` : ' · 待调度'
    }`,
    value: inst.instance_id,
    disabled: !inst.assigned_node,
  })),
)
const instanceId = ref<string | null>(props.initialInstanceId ?? null)

/** 当前选中实例(轮询后实例列表变化,按 id 找;找不到回退首个可用) */
const currentInstance = computed<DeploymentInstance | null>(() => {
  const list = props.instances
  const found = list.find((item) => item.instance_id === instanceId.value)
  if (found?.assigned_node) return found
  return list.find((item) => item.assigned_node) ?? null
})

watch(
  currentInstance,
  (inst) => {
    if (inst && inst.instance_id !== instanceId.value) instanceId.value = inst.instance_id
  },
  { immediate: true },
)

/** 回看行数选择;「加载更早」在其上翻倍重开(窗口模型无真分页) */
const LINE_OPTIONS = [200, 500, 1000]
const LINES_MAX = 4000
const lines = ref(200)

/** 跟随模式行数上限:超限从头部丢弃(保最新),防止长时间跟随高频 stdout 无界增长(内存+DOM) */
const MAX_FOLLOW_LINES = 4000

type StreamStatus = 'idle' | 'connecting' | 'running' | 'ended' | 'error'
const status = ref<StreamStatus>('idle')
const errorMessage = ref('')

/** 控制台行:raw 留原文(expand 去重比对),html 为 ANSI 上色结果(ansi_up 已做 HTML 转义,v-html 安全) */
interface ConsoleLine {
  key: number
  raw: string
  html: string
}
const consoleLines = shallowRef<ConsoleLine[]>([])
let lineKeySeed = 0

// 滚动模型:贴底自动跟随;用户上滚/按住拖动解除跟随并累计角标;scroll 事件只负责贴底重新跟随
const listRef = ref<VirtualListInst | null>(null)
const atBottom = ref(true)
const pendingCount = ref(0)

/**
 * scroll 事件只置真不置假:程序滚动(scrollToEnd)与行高测量的锚定 scrollBy 也会派发 scroll,
 * 其时点距真实底部往往 >30px(内容刚长高),若据此置假会把无人操作的跟随误打断、角标虚增。
 * 解除跟随只认用户主动意图(onUserWheel/onUserPointerDown)。
 */
function onScroll(e: Event) {
  const el = e.target as HTMLElement
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 30) {
    atBottom.value = true
    pendingCount.value = 0
  }
}

/** 用户上滚解除跟随;下滚不处理(贴底时继续跟随,距底远时本就未跟随) */
function onUserWheel(e: WheelEvent) {
  if (e.deltaY < 0) atBottom.value = false
}

/** 按住拖动(滚动条滑块/框选文本)视为离开底部的阅读意图 */
function onUserPointerDown() {
  atBottom.value = false
}

function scrollToEnd() {
  // scrollTo?.():桩/降级环境下组件实例可能未暴露 scrollTo
  listRef.value?.scrollTo?.({ position: 'bottom', behavior: 'auto' })
}

/** 跟随缓冲:帧内到达行先攒普通数组(O(1)),rAF 合帧一次 flush */
let appendBuffer: string[] = []
let rafId: number | null = null
function scheduleFlush() {
  if (rafId !== null) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    flushAppend()
  })
}

/** 合帧落地:一次 concat 追加全部缓冲行,超限一次截头,一次滚动决策 */
function flushAppend() {
  const buffered = appendBuffer
  appendBuffer = []
  if (!buffered.length) return
  const fresh = buffered.map<ConsoleLine>((text) => ({
    key: (lineKeySeed += 1),
    raw: text,
    html: ansi.ansi_to_html(text),
  }))
  let next = consoleLines.value.concat(fresh)
  if (next.length > MAX_FOLLOW_LINES) next = next.slice(next.length - MAX_FOLLOW_LINES)
  consoleLines.value = next
  if (atBottom.value) nextTick(scrollToEnd)
  else pendingCount.value += fresh.length
}

/** 追加路径:rAF 合帧;页面隐藏时 rAF 暂停,缓冲超限即同步落地(防无界增长) */
function appendLine(text: string) {
  appendBuffer.push(text)
  if (appendBuffer.length > MAX_FOLLOW_LINES) flushAppend()
  else scheduleFlush()
}

/** 取消排队中的 rAF 并立即落地(开新流/停止/卸载前,防丢已收行) */
function syncFlush() {
  if (rafId !== null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
  flushAppend()
}

/** 历史段前插:pendingEarlier 按流序(时间正序)累积,逆序整体前插保持时间序 */
function prependEarlier(texts: string[]) {
  if (!texts.length) return
  const earlier: ConsoleLine[] = []
  for (let i = texts.length - 1; i >= 0; i -= 1) {
    const text = texts[i]!
    earlier.push({ key: (lineKeySeed += 1), raw: text, html: ansi.ansi_to_html(text) })
  }
  consoleLines.value = earlier.concat(consoleLines.value)
}

function jumpToNew() {
  atBottom.value = true
  pendingCount.value = 0
  nextTick(scrollToEnd)
}

/** 全局单流:开新/切换/卸载前 abort 旧的;行缓冲同步落地防丢行 */
let controller: AbortController | null = null
function abortStream() {
  controller?.abort()
  controller = null
  syncFlush()
}

/**
 * 开流。
 * @param expand false=清空重开(切实例/切行数/重连);true=窗口向前扩(lines 翻倍):
 *   重放流按时间正序送达——未见行(更早历史)先攒缓冲,确认进入重叠段或流结束时一次性前插;
 *   已见行(重叠段)静默跳过,之后的新行 append(跟随)
 */
function openStream(expand: boolean) {
  const inst = currentInstance.value
  if (!inst?.assigned_node) return
  abortStream()
  if (!expand) {
    consoleLines.value = []
    pendingCount.value = 0
    atBottom.value = true
  }
  const seen = expand ? new Set(consoleLines.value.map((line) => line.raw)) : null
  let overlapStarted = !expand
  // 连续命中 3 条才判定重叠,防同内容行(空行/心跳)假命中
  let pendingEarlier: string[] = []
  let overlapHits = 0
  const flushEarlier = () => {
    prependEarlier(pendingEarlier)
    pendingEarlier = []
  }
  status.value = 'connecting'
  errorMessage.value = ''
  controller = new AbortController()
  const containerName = containerNameFor(props.deploymentId, inst.instance_id)
  void tailContainerLog({
    nodeId: inst.assigned_node,
    containerName,
    lines: lines.value,
    signal: controller.signal,
    onFrame: (frame) => {
      if (frame.event === 'data') {
        const raw = frame.data
        if (seen?.has(raw)) {
          overlapHits += 1
          if (!overlapStarted && overlapHits >= 3) {
            flushEarlier()
            overlapStarted = true
          }
          return
        }
        overlapHits = 0
        // 首条帧到来即视为已建流
        if (status.value !== 'running') status.value = 'running'
        if (overlapStarted) {
          appendLine(raw)
        } else {
          pendingEarlier.push(raw)
        }
        return
      }
      if (frame.event === 'error') {
        flushEarlier()
        status.value = 'error'
        errorMessage.value = frame.message
        return
      }
      // 流结束兜底 flush:新窗口与已见集完全无交集时,缓冲永远等不到重叠确认
      flushEarlier()
      status.value = 'ended'
    },
  }).catch((error: unknown) => {
    // 主动 abort 不算失败
    if (error instanceof DOMException && error.name === 'AbortError') return
    status.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : String(error)
  })
}

/** ansi_up 输出的是 HTML;行去重直接比对 raw 原文(见 ConsoleLine) */

function stopStream() {
  abortStream()
  status.value = 'idle'
}

/** 加载更早:lines 翻倍重开;返回行与已收行按内容去重前插 */
function loadEarlier() {
  if (lines.value >= LINES_MAX) return
  lines.value = Math.min(lines.value * 2, LINES_MAX)
  openStream(true)
}

/** 实例变化 → 清空重开;回看行数由 n-select @update:value 手动处理(expand 翻倍不经过它) */
watch([instanceId], () => {
  if (instanceId.value) openStream(false)
})

onMounted(() => {
  if (currentInstance.value) openStream(false)
})
onBeforeUnmount(abortStream)

const statusText = computed(() => {
  switch (status.value) {
    case 'connecting':
      return '连接中…'
    case 'running':
      return `跟随中 · 已收 ${consoleLines.value.length} 行`
    case 'ended':
      return '流已结束(容器退出或断开)'
    case 'error':
      return `连接失败:${errorMessage.value}`
    default:
      return '未连接'
  }
})
</script>

<template>
  <div class="flex flex-col">
    <!-- 工具条:实例选择 + 回看行数 + 流控制 -->
    <div class="flex items-center gap-[.8rem] mb-[.8rem] flex-wrap">
      <LmSelect
        v-model:value="instanceId"
        :options="instanceOptions"
        size="small"
        placeholder="选择实例"
        class="w-[20rem]"
      />
      <LmSelect
        :value="lines"
        :options="LINE_OPTIONS.map((v) => ({ label: `回看 ${v} 行`, value: v }))"
        size="small"
        class="w-[11rem]"
        @update:value="(v: number) => { lines = v; openStream(false) }"
      />
      <n-button size="small" :disabled="lines >= LINES_MAX" @click="loadEarlier">
        加载更早
      </n-button>
      <n-button
        v-if="status === 'running' || status === 'connecting'"
        size="small"
        quaternary
        @click="stopStream"
      >
        <template #icon>
          <n-icon :component="StopOutline" />
        </template>
        停止
      </n-button>
      <n-button
        v-else
        size="small"
        type="primary"
        ghost
        :disabled="!currentInstance"
        @click="openStream(false)"
      >
        <template #icon>
          <n-icon :component="PlayOutline" />
        </template>
        {{ status === 'ended' || status === 'error' ? '重连' : '开始' }}
      </n-button>
      <span
        class="text-xs ml-auto"
        :class="status === 'error' ? 'text-error' : 'text-tertiary'"
        :title="statusText"
      >
        {{ statusText }}
      </span>
    </div>

    <div class="relative" @pointerdown="onUserPointerDown">
      <!-- 未读角标:用户上滚/按住拖动离开底部期间到达的新行 -->
      <n-button
        v-if="pendingCount > 0"
        size="tiny"
        type="primary"
        class="absolute top-[.4rem] left-1/2 -translate-x-1/2 z-1"
        @click="jumpToNew"
      >
        <template #icon>
          <n-icon :component="ArrowDownOutline" />
        </template>
        {{ pendingCount }} 条新日志
      </n-button>

      <!-- 终端风格:固定深底(明暗主题一致),ANSI 色码自带前景色;虚拟滚动只渲染视口行 -->
      <div
        v-if="!consoleLines.length"
        class="h-[46rem] flex items-center justify-center rounded-md border border-line bg-gray-900"
      >
        <n-empty size="small" description="暂无输出" class="!text-gray-100" />
      </div>
      <!-- padding-top/bottom 必须传数字:vueuc 内部经 seemly depx() 参与滚动窗口计算,该函数
           只认数字/px——rem 字符串得 NaN,getBound(NaN) 恒返回 N/2,渲染窗口会钉死列表中部与
           真实滚动脱钩,行数超过一屏后弹窗恒空白 -->
      <n-virtual-list
        v-else
        ref="listRef"
        :items="consoleLines"
        :item-size="22"
        item-resizable
        key-field="key"
        :padding-top="6"
        :padding-bottom="6"
        class="h-[46rem] rounded-md border border-line bg-gray-900 px-[1rem] font-mono text-xs text-gray-100"
        @scroll="onScroll"
        @wheel="onUserWheel"
      >
        <template #default="{ item }">
          <div class="leading-[1.8] break-all" v-html="item.html" />
        </template>
      </n-virtual-list>
    </div>
  </div>
</template>
