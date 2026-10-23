/** 推送订阅数据,与 PushSubscription.toJSON() 结构一致,可直接 POST 给自家服务器。 */
export interface PushSubscriptionData {
  endpoint: string
  expirationTime: number | null
  keys: {
    p256dh: string
    auth: string
  }
}

/** 订阅失败原因,用于 UI 层分支提示。 */
export type PushSubscribeErrorCode =
  | 'unsupported'
  | 'permission-denied'
  | 'no-service-worker'
  | 'subscribe-failed'

export class PushSubscribeError extends Error {
  readonly code: PushSubscribeErrorCode

  constructor(code: PushSubscribeErrorCode, message: string) {
    super(message)
    this.name = 'PushSubscribeError'
    this.code = code
  }
}
