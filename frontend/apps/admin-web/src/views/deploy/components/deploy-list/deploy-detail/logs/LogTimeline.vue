<script setup lang="ts">
import { RefreshOutline, ArrowDownOutline } from '@vicons/ionicons5'
import type { VirtualListInst } from 'naive-ui'
import {
  listDeploymentLogs,
  type DeployLogItem,
  type ListDeploymentLogsParams,
} from '@/api/deployment'
import { buildDisplayRows, firstLoadPage, mergeLogsById, type DisplayRow } from './logUtils'

/**
 * 部署结构化时间线(GET /{id}/logs,文档 §3),以弹窗形式打开(入口:部署/物理节点卡片「日志」按钮):
 * 首屏 page=1 探 total、超页跳末页;增量 since=本地最后一条 created_at(含等值按 id 去重);
 * creating/deleting 5s 一轮(§8),其余终态停轮询留手动刷新;距底 <30px 自动滚,上滚挂角标。
 * nodeId 可选:从物理节点卡片进入时按节点过滤(首屏/增量均带上)。
 *
 * 性能:5000 行上限走 n-virtual-list 虚拟滚动(只渲染视口);行内时间/tag/节点文本由
 * buildDisplayRows 一次预计算进对象,渲染只读字段;30s 心跳只重算该 computed,不逐行调函数。
 */
const props = defineProps<{
  deploymentId: string
  /** 打开弹窗时的部署状态快照(creating 增量轮询;终态停留手动刷新) */
  status: string
  /** 按节点过滤(物理节点入口);不传为全部署 */
  nodeId?: string | null
}>()

const logs = shallowRef<DeployLogItem[]>([])
/** 渲染行:文本预计算进对象;读 timeTick 建立依赖,30s 心跳触发重算(一次 computed 全量重建) */
const timeTick = ref(0)
const displayRows = computed<DisplayRow[]>(() => {
  void timeTick.value
  return buildDisplayRows(logs.value, Date.now())
})

/** 相对时间重算节拍:30s 一次;页面不可见时暂停,恢复可见立即刷一次 */
let timeTimer: ReturnType<typeof setInterval> | null = null
function clearTimeTimer() {
  if (timeTimer) {
    clearInterval(timeTimer)
    timeTimer = null
  }
}
function startTimeTimer() {
  clearTimeTimer()
  if (document.hidden) return
  timeTimer = setInterval(() => {
    timeTick.value += 1
  }, 30_000)
}

const loading = ref(false)
/** 日志保留上限(尾部截断):creating/deleting 长会话跟随轮询防无界增长,10 页量足够回看 */
const MAX_LOGS = 5000
/** 过滤参数(增量轮询原样带上,防止混入过滤外新行);关键字回车/清空才提交,不逐键触发 */
const level = ref<string | null>(null)
const keywordInput = ref('')
const keyword = ref('')
const levelOptions = [
  { label: '全部级别', value: '' },
  { label: 'debug', value: 'debug' },
  { label: 'info', value: 'info' },
  { label: 'warn', value: 'warn' },
  { label: 'error', value: 'error' },
]

function submitKeyword() {
  keyword.value = keywordInput.value.trim()
}

function filterParams(): ListDeploymentLogsParams {
  return {
    node_id: props.nodeId ?? null,
    level: level.value || null,
    search: keyword.value.trim() || null,
  }
}

// 滚动模型:贴底自动滚;用户上滚时不打断,顶部角标累计未读
const listRef = ref<VirtualListInst | null>(null)
const atBottom = ref(true)
const pendingCount = ref(0)

function onScroll(e: Event) {
  const el = e.target as HTMLElement
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 30
  if (atBottom.value) pendingCount.value = 0
}

function scrollToEnd() {
  // scrollTo?.():桩/降级环境下组件实例可能未暴露 scrollTo
  listRef.value?.scrollTo?.({ position: 'bottom', behavior: 'auto' })
}

/** 追加新行后的滚动决策:贴底 → 滚底;上滚中 → 角标 */
function afterAppend(fresh: number) {
  if (fresh <= 0) return
  if (atBottom.value) nextTick(scrollToEnd)
  else pendingCount.value += fresh
}

function jumpToNew() {
  atBottom.value = true
  pendingCount.value = 0
  nextTick(scrollToEnd)
}

/** 首屏:page=1 探 total,超一页跳末页拿最新一段 */
async function loadFirst() {
  loading.value = true
  try {
    const probe = await listDeploymentLogs(props.deploymentId, {
      ...filterParams(),
      page: 1,
      page_size: 500,
    })
    let target = probe
    if (probe.total > 500 && probe.total_pages > 1) {
      target = await listDeploymentLogs(props.deploymentId, {
        ...filterParams(),
        page: firstLoadPage(probe.total, 500),
        page_size: 500,
      })
    }
    const prev = logs.value.length
    logs.value = target.items
    afterAppend(target.items.length - prev)
  } catch {
    // HTTP 层已统一提示;保留已有行
  } finally {
    loading.value = false
  }
}

/** 增量:since=本地最后一条 created_at(含等值 → 按 id 去重);新增数按 incoming 去重计,不受截尾影响 */
async function loadIncrement() {
  const last = logs.value[logs.value.length - 1]
  if (!last) {
    await loadFirst()
    return
  }
  try {
    const page = await listDeploymentLogs(props.deploymentId, {
      ...filterParams(),
      since: last.created_at,
      page_size: 500,
    })
    const seenIds = new Set(logs.value.map((item) => item.id))
    const fresh = page.items.filter((item) => !seenIds.has(item.id)).length
    logs.value = mergeLogsById(logs.value, page.items, MAX_LOGS)
    afterAppend(fresh)
  } catch {
    // 增量失败静默,下轮再试
  }
}

// 轮询:creating/deleting 5s;终态停(手动刷新);页面不可见暂停
// disposed 守卫:弹窗关闭(卸载)后在途请求的 continuation 不得重新挂表,防止定时器无限自续
const active = computed(() => props.status === 'creating' || props.status === 'deleting')
let disposed = false
let timer: ReturnType<typeof setTimeout> | null = null
function clearTimer() {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
}
function scheduleNext() {
  if (disposed) return
  clearTimer()
  if (document.hidden || !active.value) return
  timer = setTimeout(tick, 5000)
}
async function tick() {
  await loadIncrement()
  scheduleNext()
}
function onVisibilityChange() {
  if (document.hidden) {
    clearTimer()
    clearTimeTimer()
  } else {
    scheduleNext()
    timeTick.value += 1
    startTimeTimer()
  }
}

watch(active, scheduleNext)
onMounted(() => {
  void loadFirst()
  scheduleNext()
  startTimeTimer()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  disposed = true
  clearTimer()
  clearTimeTimer()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

/** 过滤变化:重载首屏 */
watch([level, keyword], () => void loadFirst())

/** 级别 → n-tag 配色 */
const LEVEL_TAG_TYPE: Record<string, 'default' | 'info' | 'warning' | 'error'> = {
  debug: 'default',
  info: 'info',
  warn: 'warning',
  error: 'error',
}
</script>

<template>
  <div class="flex flex-col">
    <!-- 工具条:级别过滤 + 关键字 + 手动刷新 -->
    <div class="flex items-center gap-[.8rem] mb-[.8rem] flex-wrap">
      <LmSelect
        :value="level ?? ''"
        :options="levelOptions"
        size="small"
        class="w-[11rem]"
        @update:value="level = $event || null"
      />
      <n-input
        v-model:value="keywordInput"
        size="small"
        clearable
        placeholder="消息关键字,回车搜索"
        class="!w-[18rem]"
        @keyup.enter="submitKeyword"
        @clear="submitKeyword"
      />
      <n-button size="small" quaternary :loading="loading" @click="loadFirst()">
        <template #icon>
          <n-icon :component="RefreshOutline" />
        </template>
        刷新
      </n-button>
      <span class="text-xs text-tertiary ml-auto whitespace-nowrap">共 {{ logs.length }} 条</span>
    </div>

    <div class="relative">
      <!-- 未读角标:用户上滚期间到达的新日志 -->
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

      <div
        v-if="!logs.length && !loading"
        class="h-[46rem] flex items-center justify-center rounded-md border border-line bg-fill-1"
      >
        <n-empty size="small" description="暂无日志" class="font-sans" />
      </div>
      <!-- 虚拟滚动:只渲染视口内行(5000 行上限下 DOM 恒定);itemResizable 适配换行变高行 -->
      <n-virtual-list
        v-else
        ref="listRef"
        :items="displayRows"
        :item-size="27"
        item-resizable
        key-field="key"
        class="h-[46rem] rounded-md border border-line bg-fill-1 font-mono text-xs"
        @scroll="onScroll"
      >
        <!-- vueuc 虚拟列表插槽取返回 vnode 数组首元素:模板必须单根(v-if/v-else 平级双分支会留注释占位被误取) -->
        <template #default="{ item }">
          <div>
            <!-- 时间间隔分组行:相邻两条 > 10 分钟时插入,不参与等宽列对齐 -->
            <div
              v-if="item.kind === 'group'"
              class="flex items-center gap-[.8rem] py-[.2rem] text-tertiary select-none font-sans"
            >
              <span class="flex-1 border-t border-line" />
              <span class="shrink-0 text-xs">{{ item.label }} · {{ item.gapText }}</span>
              <span class="flex-1 border-t border-line" />
            </div>
            <!-- 日志行:时间列相对时间(同秒折叠为居中圆点),悬浮给完整时间;时间列右侧为时间线圆点+连接线 -->
            <div
              v-else
              class="relative flex gap-[.8rem] items-start leading-[1.8] pl-[.5rem] pr-[.5rem] py-[.2rem] rounded-[.4rem] hover:bg-fill-2"
            >
              <span
                class="text-tertiary shrink-0 w-[6.5em] text-right tabular-nums"
                :class="{ 'opacity-30': item.timeText === '·' }"
                :title="item.timeTitle"
              >
                {{ item.timeText }}
              </span>
              <!-- 时间线:竖线贯穿整行,圆点与时间文本垂直居中对齐 -->
              <span class="absolute left-[7.3em] top-0 bottom-0 w-px bg-line" aria-hidden="true" />
              <span
                class="absolute left-[7.1em] top-[.7rem] w-[.6rem] h-[.6rem] my-[.3rem] rounded-full bg-line-strong shrink-0"
                aria-hidden="true"
              />
              <n-tag
                size="tiny"
                :bordered="false"
                :type="LEVEL_TAG_TYPE[item.item.level] ?? 'default'"
                class="font-mono ml-[.5rem]"
              >
                {{ item.tagText }}
              </n-tag>
              <span
                v-if="item.nodeText"
                class="shrink-0 truncate max-w-[10rem] font-mono tabular-nums"
                :title="item.nodeText"
              >
                {{ item.nodeText }}
              </span>
              <span class="break-all" :class="{ 'text-error': item.item.level === 'error' }">
                {{ item.item.message ?? '' }}
              </span>
            </div>
          </div>
        </template>
      </n-virtual-list>
    </div>
  </div>
</template>
