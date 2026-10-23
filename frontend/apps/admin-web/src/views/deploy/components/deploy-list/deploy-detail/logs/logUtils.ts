import type { DeployLogItem } from '@/api/deployment'
import { NODE_STEP_LABEL } from '../useStore'

/**
 * 日志区纯函数:时间线增量合并、首屏分页决策、tail-log 容器名拼接与控制台窗口合并。
 * 对接文档 §3(升序分页 + since 含等值按 id 去重)与 §4(容器名规则、窗口模型去重前插)。
 */

/** 行步骤标题:词表映射(文档 §4);step=start 按 extra.action 细化启动/重启(文档 §7) */
export function stepTitle(step: string, extra: Record<string, unknown> | null): string {
  if (step === 'start') return extra?.action === 'restart' ? '重启' : '启动'
  return NODE_STEP_LABEL[step] ?? step
}

/** 行节点展示名:extra.node_hostname > extra.node_ip > node_id 短 hash(文档 §7);编排事件(无 node)为 null */
export function nodeDisplayName(
  item: Pick<DeployLogItem, 'node_id' | 'extra'>,
): string | null {
  const hostname = item.extra?.node_hostname
  if (typeof hostname === 'string' && hostname) return hostname
  const ip = item.extra?.node_ip
  if (typeof ip === 'string' && ip) return ip
  return item.node_id ? item.node_id.slice(0, 8) : null
}

/** 增量合并:按行 id 去重追加(since 含等值会带回本地最后一条);maxLogs 超限时截尾,防跟随轮询长会话无界增长 */
export function mergeLogsById(
  existing: DeployLogItem[],
  incoming: DeployLogItem[],
  maxLogs?: number,
): DeployLogItem[] {
  const seen = new Set(existing.map((item) => item.id))
  const fresh = incoming.filter((item) => !seen.has(item.id))
  const merged = fresh.length ? [...existing, ...fresh] : existing
  if (maxLogs === undefined || merged.length <= maxLogs) return merged
  return merged.slice(merged.length - maxLogs)
}

/** 首屏分页决策:拉 page=1 探 total;超一页则跳末页取最新一段(日志只看最近) */
export function firstLoadPage(total: number, pageSize: number): number {
  if (total <= pageSize) return 1
  return Math.ceil(total / pageSize)
}

/** tail-log 容器名:`llmpod-<deployment_id 前 8 位>-<instance_id>`(文档 §1 不变量) */
export function containerNameFor(deploymentId: string, instanceId: string): string {
  return `llmpod-${deploymentId.slice(0, 8)}-${instanceId}`
}

/**
 * 控制台窗口合并:重发更大 lines 后整段重取,与已收行按内容去重、保序前插。
 * 返回 null 表示无新增(全部重复);新行可能夹在中间(历史更早 + 跟随的新行),按新流顺序整体重排:
 * 取新流中第一个未出现行的位置切分,前段前插、后段追加,保证与 docker 时间序一致。
 */
export function mergeConsoleLines(existing: string[], incoming: string[]): string[] | null {
  if (!incoming.length) return null
  const seen = new Set(existing)
  if (!incoming.some((line) => !seen.has(line))) return null
  // 常态:新流 = [更早历史..., 已有窗口开头..., (可能的新跟随行) ];首个已见行即历史/重叠分界
  let splitAt = incoming.findIndex((line) => seen.has(line))
  if (splitAt < 0) splitAt = incoming.length
  const earlier = incoming.slice(0, splitAt)
  // 重叠段之后可能还有跟随新行:按顺序过滤掉已见行
  const freshRest = incoming.slice(splitAt).filter((line) => !seen.has(line))
  return [...earlier, ...existing, ...freshRest]
}

/** 时间线行:普通日志行 */
export type LogRow = {
  kind: 'log'
  item: DeployLogItem
}

/** 时间线行:静默间隔分组(相邻两条间隔 > GROUP_GAP_MINUTES 时插入) */
export type GroupRow = {
  kind: 'group'
  /** 分组起始时间(后一条):与前一条同天 HH:mm,跨天 MM-DD HH:mm */
  label: string
  /** 相对前一条的间隔描述 */
  gapText: string
}

export type TimelineRow = LogRow | GroupRow

/** 相邻两条日志间隔超过该分钟数时,在时间线中插入分组 */
export const GROUP_GAP_MINUTES = 10

const pad2 = (n: number) => String(n).padStart(2, '0')

/** 判断给定时间是否落在 now 对应本地日期的前一天(自然日) */
function isYesterday(d: Date, now: number): boolean {
  const nowDate = new Date(now)
  const yesterday = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate() - 1)
  return (
    d.getFullYear() === yesterday.getFullYear() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getDate() === yesterday.getDate()
  )
}

/** 相对时间:刚刚 / N 秒前 / N 分钟前 / N 小时前 / 昨天 HH:mm / MM-DD HH:mm;未来按刚刚;非法原样 */
export function formatRelativeTime(iso: string, now: number): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diff = now - d.getTime()
  if (diff < 5000) return '刚刚'
  if (diff < 60000) return `${Math.floor(diff / 1000)} 秒前`
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  return isYesterday(d, now) ? `昨天 ${hm}` : `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`
}

/** 绝对时间:YYYY-MM-DD HH:mm:ss;非法原样 */
export function formatAbsoluteTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

/** 同秒判断:按秒(截断毫秒)比较;任一非法返回 false */
export function isSameSecond(aIso: string, bIso: string): boolean {
  const a = new Date(aIso)
  const b = new Date(bIso)
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return false
  return Math.floor(a.getTime() / 1000) === Math.floor(b.getTime() / 1000)
}

/** 分组时间标签:与前一条同天 HH:mm,跨天 MM-DD HH:mm */
function groupLabel(prevDate: Date, curDate: Date): string {
  const hm = `${pad2(curDate.getHours())}:${pad2(curDate.getMinutes())}`
  const sameDay =
    prevDate.getFullYear() === curDate.getFullYear() &&
    prevDate.getMonth() === curDate.getMonth() &&
    prevDate.getDate() === curDate.getDate()
  return sameDay ? hm : `${pad2(curDate.getMonth() + 1)}-${pad2(curDate.getDate())} ${hm}`
}

/** 间隔描述:N 分钟后 / N 小时 N 分钟后(整小时不带分钟) / N 天前 */
function describeGap(diff: number): string {
  const minutes = Math.floor(diff / 60000)
  if (minutes < 60) return `${minutes} 分钟后`
  if (diff < 86400000) {
    const hours = Math.floor(diff / 3600000)
    const restMinutes = Math.floor((diff % 3600000) / 60000)
    return restMinutes === 0 ? `${hours} 小时后` : `${hours} 小时 ${restMinutes} 分钟后`
  }
  return `${Math.floor(diff / 86400000)} 天前`
}

/** 时间线分组:相邻两条 created_at 差 > GROUP_GAP_MINUTES 分钟时,在后一条之前插入分组行 */
export function buildTimelineRows(logs: DeployLogItem[]): TimelineRow[] {
  const rows: TimelineRow[] = []
  for (let i = 0; i < logs.length; i++) {
    const cur = logs[i]!
    if (i > 0) {
      const prev = logs[i - 1]!
      const prevTime = new Date(prev.created_at)
      const curTime = new Date(cur.created_at)
      if (!Number.isNaN(prevTime.getTime()) && !Number.isNaN(curTime.getTime())) {
        const diff = curTime.getTime() - prevTime.getTime()
        if (diff > GROUP_GAP_MINUTES * 60000) {
          rows.push({ kind: 'group', label: groupLabel(prevTime, curTime), gapText: describeGap(diff) })
        }
      }
    }
    rows.push({ kind: 'log', item: cur })
  }
  return rows
}

/** 渲染行(n-virtual-list 的 items, keyField="key"):时间列/tag/节点文本全部预计算,渲染时只读字段零函数调用 */
export type DisplayRow =
  | {
      key: string | number
      kind: 'log'
      item: DeployLogItem
      /** 相对时间;与上一行(须为 log 行)同秒折叠为 · */
      timeText: string
      /** 悬浮完整时间 */
      timeTitle: string
      /** 步骤 tag 文本 */
      tagText: string
      /** 节点展示名;编排事件(无 node)为 null */
      nodeText: string | null
    }
  | { key: string | number; kind: 'group'; label: string; gapText: string }

/**
 * 构建渲染行:在 buildTimelineRows 结构上预计算全部行内文本。
 * 相对时间依赖 now,由调用方传入(30s 心跳驱动重算),避免模板渲染函数逐行调用
 * formatRelativeTime/formatAbsoluteTime(5000 行时每轮渲染上万次函数调用)。
 */
export function buildDisplayRows(logs: DeployLogItem[], now: number): DisplayRow[] {
  const rows = buildTimelineRows(logs)
  const out: DisplayRow[] = []
  let prev: TimelineRow | undefined
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]!
    if (row.kind === 'group') {
      out.push({ key: `group-${index}`, kind: 'group', label: row.label, gapText: row.gapText })
    } else {
      const folded = prev?.kind === 'log' && isSameSecond(prev.item.created_at, row.item.created_at)
      out.push({
        key: row.item.id,
        kind: 'log',
        item: row.item,
        timeText: folded ? '·' : formatRelativeTime(row.item.created_at, now),
        timeTitle: formatAbsoluteTime(row.item.created_at),
        tagText: stepTitle(row.item.step, row.item.extra),
        nodeText: nodeDisplayName(row.item),
      })
    }
    prev = row
  }
  return out
}
