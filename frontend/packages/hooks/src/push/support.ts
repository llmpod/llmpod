import type { PushSubscribeErrorCode } from './types'

/** 检测当前环境是否支持 Web Push(secure context + ServiceWorker + PushManager + Notification)。 */
export function getWebPushSupport(): { supported: boolean; reasons: PushSubscribeErrorCode[] } {
  const reasons: PushSubscribeErrorCode[] = []
  if (typeof window === 'undefined' || !window.isSecureContext) {
    reasons.push('unsupported')
  }
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) {
    reasons.push('no-service-worker')
  }
  if (typeof window === 'undefined' || !('PushManager' in window)) {
    reasons.push('unsupported')
  }
  if (typeof Notification === 'undefined') {
    reasons.push('permission-denied')
  }
  return { supported: reasons.length === 0, reasons }
}

/** 当前通知权限状态;环境不支持 Notification 时返回 'denied'。 */
export function getNotificationPermission(): NotificationPermission {
  if (typeof Notification === 'undefined') return 'denied'
  return Notification.permission
}
