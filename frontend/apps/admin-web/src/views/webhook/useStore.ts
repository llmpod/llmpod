import { ref } from 'vue'

/** Webhook 事件类型 */
export type WebhookEvent = 'model.deploy.completed' | 'tenant.balance.low' | 'node.status.changed'

/** 列表行 / 本地数据记录 */
export interface Webhook {
  id: number
  name: string
  event: WebhookEvent
  url: string
  enabled: boolean
  lastTriggeredAt: string
}

/** 事件类型表单选项 */
export const eventFormOptions = [
  { label: 'model.deploy.completed', value: 'model.deploy.completed' },
  { label: 'tenant.balance.low', value: 'tenant.balance.low' },
  { label: 'node.status.changed', value: 'node.status.changed' },
]

/** 模块级单例:表格与表单共享同一份数据与变更通知 */
const records = ref<Webhook[]>([
  { id: 1, name: '模型部署完成', event: 'model.deploy.completed', url: 'https://hook.company.com/deploy', enabled: true, lastTriggeredAt: '2026-07-03 09:15' },
  { id: 2, name: '租户余额不足', event: 'tenant.balance.low', url: 'https://hook.company.com/deploy', enabled: true, lastTriggeredAt: '2026-07-03 09:15' },
  { id: 3, name: '节点状态变更', event: 'node.status.changed', url: 'https://hook.company.com/deploy', enabled: true, lastTriggeredAt: '2026-07-03 09:15' },
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
 * Webhook 数据 store:表格请求、增删改均走此单例,
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
    webhook: Omit<Webhook, 'id' | 'enabled' | 'lastTriggeredAt'> & { id?: number },
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    await delay(300)
    if (webhook.id) {
      const row = records.value.find((r) => r.id === webhook.id)
      if (!row) throw new Error('Webhook不存在')
      Object.assign(row, webhook)
      notify()
      return { mode: 'update', name: row.name }
    }
    const id = Math.max(0, ...records.value.map((r) => r.id)) + 1
    records.value.unshift({ ...webhook, id, enabled: true, lastTriggeredAt: '-' })
    notify()
    return { mode: 'create', name: webhook.name }
  }

  /** 发送测试事件 */
  const test = async (id: number): Promise<void> => {
    await delay(300)
    const row = records.value.find((r) => r.id === id)
    if (!row) throw new Error('Webhook不存在')
    row.lastTriggeredAt = '2026-07-03 09:15'
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { records, request, save, test, subscribe }
}
