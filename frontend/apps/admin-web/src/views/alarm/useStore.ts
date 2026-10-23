import { ref } from 'vue'

/** 监控指标:月度消费 / 日消费 / 账户余额 */
export type AlarmMetric = 'monthly' | 'daily' | 'balance'

/** 通知方式:邮件 / 飞书 */
export type AlarmChannel = 'email' | 'feishu'

/** 列表行 / 本地数据记录 */
export interface AlarmRule {
  id: number
  name: string
  metric: AlarmMetric
  /** 触发比较符:小于 / 大于 */
  comparator: '<' | '>'
  /** 阈值(元) */
  threshold: number
  channels: AlarmChannel[]
  enabled: boolean
}

/** 指标文案映射 */
export const alarmMetricMap: Record<AlarmMetric, string> = {
  monthly: '月度消费',
  daily: '日消费',
  balance: '账户余额',
}

/** 指标表单选项 */
export const metricFormOptions = [
  { label: '月度消费', value: 'monthly' },
  { label: '日消费', value: 'daily' },
  { label: '账户余额', value: 'balance' },
]

/** 比较符表单选项 */
export const comparatorFormOptions = [
  { label: '小于 <', value: '<' },
  { label: '大于 >', value: '>' },
]

/** 通知方式文案映射 */
export const alarmChannelMap: Record<AlarmChannel, string> = {
  email: '邮件',
  feishu: '飞书',
}

/** 通知方式表单选项 */
export const channelFormOptions = [
  { label: '邮件', value: 'email' },
  { label: '飞书', value: 'feishu' },
]

/** 模块级单例:表格与表单共享同一份数据与变更通知 */
const records = ref<AlarmRule[]>([
  { id: 1, name: '月度预算超限', metric: 'monthly', comparator: '<', threshold: 500, channels: ['email', 'feishu'], enabled: true },
  { id: 2, name: '余额不足', metric: 'balance', comparator: '<', threshold: 50, channels: ['feishu'], enabled: true },
  { id: 3, name: '日消费突增', metric: 'daily', comparator: '<', threshold: 200, channels: ['email'], enabled: true },
])

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/** 模拟远程延迟 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 告警规则数据 store:表格请求、增删改均走此单例,
 * 写操作完成后 notify 触发表格联动刷新。
 */
export function useStore() {
  /** 分页请求(供 useTable.request) */
  const request = async (params: { page: number; pageSize: number }) => {
    await delay(300)
    const start = (params.page - 1) * params.pageSize
    return {
      data: records.value.slice(start, start + params.pageSize),
      total: records.value.length,
    }
  }

  /** 新建 / 编辑:id 存在为编辑,否则新建 */
  const save = async (
    rule: Omit<AlarmRule, 'id' | 'enabled'> & { id?: number },
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    await delay(300)
    if (rule.id) {
      const row = records.value.find((r) => r.id === rule.id)
      if (!row) throw new Error('规则不存在')
      Object.assign(row, rule)
      notify()
      return { mode: 'update', name: row.name }
    }
    const id = Math.max(0, ...records.value.map((r) => r.id)) + 1
    records.value.unshift({ ...rule, id, enabled: true })
    notify()
    return { mode: 'create', name: rule.name }
  }

  /** 删除规则 */
  const remove = async (id: number): Promise<void> => {
    await delay(300)
    const index = records.value.findIndex((r) => r.id === id)
    if (index < 0) throw new Error('规则不存在')
    records.value.splice(index, 1)
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { records, request, save, remove, subscribe }
}
