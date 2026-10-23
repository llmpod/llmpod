import { ref } from 'vue'

/** 套餐类型:包月 / 包年 */
export type PackageType = 'monthly' | 'yearly'

/** 列表行 / 本地数据记录 */
export interface Package {
  id: number
  name: string
  type: PackageType
  price: number
  tokenQuota: string
  qps: number
  concurrency: number
  enabled: boolean
  subscribers: number
}

/** 表单模型:id 可选(新增时无);数值字段允许 null(未填写) */
export interface PackageFormValues {
  id?: number
  name: string
  type: PackageType | ''
  price: number | null
  tokenQuota: string
  qps: number | null
  concurrency: number | null
}

/** 套餐类型选项(表单用) */
export const typeFormOptions = [
  { label: '包月', value: 'monthly' },
  { label: '包年', value: 'yearly' },
]

/** 套餐类型文案映射 */
export const packageTypeMap: Record<PackageType, string> = {
  monthly: '包月',
  yearly: '包年',
}

/** 模块级单例:表格与弹窗共享同一份数据与变更通知 */
const records = ref<Package[]>([
  { id: 1, name: '基础版', type: 'monthly', price: 99, tokenQuota: '1000万', qps: 200, concurrency: 200, enabled: true, subscribers: 12 },
  { id: 2, name: '专业版', type: 'monthly', price: 299, tokenQuota: '5000万', qps: 100, concurrency: 100, enabled: true, subscribers: 8 },
  { id: 3, name: '企业版', type: 'monthly', price: 999, tokenQuota: '3亿', qps: 50, concurrency: 50, enabled: true, subscribers: 3 },
  { id: 4, name: '团队版', type: 'monthly', price: 599, tokenQuota: '1亿', qps: 80, concurrency: 80, enabled: true, subscribers: 5 },
  { id: 5, name: '体验版', type: 'monthly', price: 0, tokenQuota: '100万', qps: 20, concurrency: 10, enabled: false, subscribers: 45 },
  { id: 6, name: '入门版', type: 'monthly', price: 49, tokenQuota: '500万', qps: 300, concurrency: 150, enabled: false, subscribers: 20 },
  { id: 7, name: '旗舰版', type: 'yearly', price: 9999, tokenQuota: '50亿', qps: 30, concurrency: 30, enabled: true, subscribers: 1 },
  { id: 8, name: '定制版', type: 'yearly', price: 19999, tokenQuota: '不限量', qps: 10, concurrency: 10, enabled: true, subscribers: 2 },
])
let nextId = records.value.length + 1

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
 * 套餐数据 store:表格请求、表单保存均走此单例,
 * 弹窗组件内部即可完成「保存 + 提示 + 关闭」,无需父级回调。
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

  /** 新增或更新:依据 model.id 判断;返回操作结果供调用方提示 */
  const save = async (
    model: PackageFormValues,
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    await delay(300)
    let mode: 'create' | 'update'
    if (model.id) {
      const target = records.value.find((r) => r.id === model.id)
      if (target) {
        Object.assign(target, {
          name: model.name,
          type: model.type as PackageType,
          price: model.price ?? 0,
          tokenQuota: model.tokenQuota,
          qps: model.qps ?? 0,
          concurrency: model.concurrency ?? 0,
        })
      }
      mode = 'update'
    } else {
      records.value.unshift({
        id: nextId++,
        name: model.name,
        type: model.type as PackageType,
        price: model.price ?? 0,
        tokenQuota: model.tokenQuota,
        qps: model.qps ?? 50,
        concurrency: model.concurrency ?? 10,
        enabled: true,
        subscribers: 0,
      })
      mode = 'create'
    }
    notify()
    return { mode, name: model.name }
  }

  /** 删除套餐 */
  const remove = async (id: number): Promise<void> => {
    await delay(300)
    records.value = records.value.filter((r) => r.id !== id)
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
