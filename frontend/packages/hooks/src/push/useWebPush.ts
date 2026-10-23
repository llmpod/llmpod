import { computed, ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'

import { getNotificationPermission, getWebPushSupport } from './support'
import { subscribePush, unsubscribePush } from './subscription'
import type { PushSubscribeErrorCode, PushSubscriptionData } from './types'

export interface UseWebPushOptions {
  /** VAPID 公钥(base64url),调用 subscribe 时必须提供 */
  applicationServerKey: string
  /** 订阅成功后的回调(通常用于把 subscription 发给自家服务器) */
  onSubscribed?: (subscription: PushSubscriptionData) => void | Promise<void>
  /** 退订成功后的回调(通常用于通知服务器删除订阅) */
  onUnsubscribed?: () => void | Promise<void>
}

export interface UseWebPushReturn {
  /** 环境是否支持 Web Push */
  supported: ComputedRef<boolean>
  /** 当前通知权限 */
  permission: ComputedRef<NotificationPermission>
  /** 是否正在订阅/退订中 */
  loading: Ref<boolean>
  /** 最近一次错误码 */
  errorCode: Ref<PushSubscribeErrorCode | undefined>
  /** 当前订阅数据 */
  subscription: Ref<PushSubscriptionData | null>
  /** 发起订阅;已订阅时直接返回缓存 */
  subscribe: () => Promise<PushSubscriptionData | null>
  /** 取消订阅 */
  unsubscribe: () => Promise<boolean>
}

/**
 * Web Push 组合式函数:封装订阅/退订/权限状态,业务侧只管调 subscribe/unsubscribe。
 * 注意:sw.js 仍需各 app 自行注册(放 public 目录),本 hook 只处理 Push 订阅生命周期。
 */
export function useWebPush(options: UseWebPushOptions): UseWebPushReturn {
  const loading = ref(false)
  const errorCode = ref<PushSubscribeErrorCode>()
  const subscription = ref<PushSubscriptionData | null>(null)

  const support = getWebPushSupport()
  const supported = computed(() => support.supported)
  const permission = computed(() => getNotificationPermission())

  async function subscribe(): Promise<PushSubscriptionData | null> {
    if (loading.value) return subscription.value
    loading.value = true
    errorCode.value = undefined
    try {
      const result = await subscribePush(options.applicationServerKey)
      subscription.value = result
      if (result && options.onSubscribed) await options.onSubscribed(result)
      return result
    }
    catch (error) {
      errorCode.value = (error as { code?: PushSubscribeErrorCode }).code ?? 'subscribe-failed'
      return null
    }
    finally {
      loading.value = false
    }
  }

  async function unsubscribe(): Promise<boolean> {
    if (loading.value) return false
    loading.value = true
    errorCode.value = undefined
    try {
      const result = await unsubscribePush()
      if (result) {
        subscription.value = null
        if (options.onUnsubscribed) await options.onUnsubscribed()
      }
      return result
    }
    finally {
      loading.value = false
    }
  }

  return {
    supported,
    permission,
    loading,
    errorCode,
    subscription,
    subscribe,
    unsubscribe,
  }
}
