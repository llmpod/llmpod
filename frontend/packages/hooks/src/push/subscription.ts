import { getNotificationPermission, getWebPushSupport } from './support'
import { PushSubscribeError } from './types'
import type { PushSubscriptionData } from './types'
import { urlBase64ToUint8Array } from './vapid'

/** 模块级缓存:同一会话内订阅结果复用,避免重复请求权限与重复网络往返。 */
let cached: Promise<PushSubscriptionData | null> | null = null

async function doSubscribe(applicationServerKey: string): Promise<PushSubscriptionData | null> {
  const { supported } = getWebPushSupport()
  if (!supported) {
    throw new PushSubscribeError('unsupported', '当前环境不支持 Web Push(需要 HTTPS 与较新浏览器)')
  }

  if (getNotificationPermission() === 'denied') {
    throw new PushSubscribeError('permission-denied', '通知权限已被拒绝,请在浏览器设置中开启')
  }

  const registration = await navigator.serviceWorker.ready
  if (!registration) {
    throw new PushSubscribeError('no-service-worker', 'Service Worker 未就绪,请先注册 sw')
  }

  const existing = await registration.pushManager.getSubscription()
  if (existing) return existing.toJSON() as PushSubscriptionData

  const key = urlBase64ToUint8Array(applicationServerKey)
  if (!key) {
    throw new PushSubscribeError('subscribe-failed', 'VAPID 公钥格式非法')
  }

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') {
    throw new PushSubscribeError('permission-denied', '用户未授权通知权限')
  }

  try {
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: key as BufferSource,
    })
    return subscription.toJSON() as PushSubscriptionData
  }
  catch (error) {
    throw new PushSubscribeError(
      'subscribe-failed',
      error instanceof Error ? error.message : 'pushManager.subscribe 调用失败',
    )
  }
}

/**
 * 订阅 Web Push,返回可发给自家服务器的订阅数据。
 * 同一会话内多次调用复用同一 Promise;需强制重订阅请先 unsubscribePush()。
 */
export function subscribePush(applicationServerKey: string): Promise<PushSubscriptionData | null> {
  if (!cached) {
    cached = doSubscribe(applicationServerKey)
  }
  return cached
}

/** 取消当前订阅并清空缓存;成功返回 true,无订阅或失败返回 false。 */
export async function unsubscribePush(): Promise<boolean> {
  const { supported } = getWebPushSupport()
  if (!supported) return false

  try {
    const registration = await navigator.serviceWorker.ready
    const subscription = await registration.pushManager.getSubscription()
    if (!subscription) {
      cached = null
      return false
    }
    const result = await subscription.unsubscribe()
    cached = null
    return result
  }
  catch {
    return false
  }
}

/** 读取当前已存在的订阅,不触发订阅流程。 */
export async function getPushSubscription(): Promise<PushSubscriptionData | null> {
  const { supported } = getWebPushSupport()
  if (!supported) return null

  try {
    const registration = await navigator.serviceWorker.ready
    const subscription = await registration.pushManager.getSubscription()
    return subscription ? (subscription.toJSON() as PushSubscriptionData) : null
  }
  catch {
    return null
  }
}

/** 清空订阅缓存(测试或手动重订阅时使用)。 */
export function clearPushSubscriptionCache(): void {
  cached = null
}
