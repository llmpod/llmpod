// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  PushSubscribeError,
  clearPushSubscriptionCache,
  getNotificationPermission,
  getPushSubscription,
  getWebPushSupport,
  subscribePush,
  unsubscribePush,
  urlBase64ToUint8Array,
} from '../src/push'

function makeSubscription(overrides: Partial<PushSubscriptionJSON> = {}) {
  return {
    endpoint: 'https://push.example.com/sub/1',
    expirationTime: null,
    keys: { p256dh: 'p256dh-key', auth: 'auth-key' },
    ...overrides,
  }
}

function mockPushManager(subscription: PushSubscription | null) {
  return {
    getSubscription: vi.fn().mockResolvedValue(subscription),
    subscribe: vi.fn().mockResolvedValue(subscription),
  }
}

function mockServiceWorkerRegistration(pushManager: ReturnType<typeof mockPushManager>) {
  return { pushManager } as unknown as ServiceWorkerRegistration
}

describe('urlBase64ToUint8Array', () => {
  it('converts base64url string to Uint8Array', () => {
    const result = urlBase64ToUint8Array('aGVsbG8')
    expect(result).not.toBeNull()
    expect(Array.from(result!)).toEqual([...new TextEncoder().encode('hello')])
  })

  it('handles padding correctly', () => {
    expect(urlBase64ToUint8Array('YQ')).not.toBeNull()
    expect(urlBase64ToUint8Array('YWI')).not.toBeNull()
    expect(urlBase64ToUint8Array('YWJj')).not.toBeNull()
  })

  it('returns null for invalid input', () => {
    expect(urlBase64ToUint8Array('!!!')).toBeNull()
    expect(urlBase64ToUint8Array('')).toBeNull()
  })
})

describe('getWebPushSupport', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports unsupported in non-secure context', () => {
    vi.stubGlobal('window', { isSecureContext: false })
    const { supported, reasons } = getWebPushSupport()
    expect(supported).toBe(false)
    expect(reasons).toContain('unsupported')
  })

  it('reports supported when all APIs present', () => {
    vi.stubGlobal('window', { isSecureContext: true, PushManager: class {} })
    vi.stubGlobal('navigator', { serviceWorker: {} })
    vi.stubGlobal('Notification', { permission: 'default' })
    const { supported, reasons } = getWebPushSupport()
    expect(supported).toBe(true)
    expect(reasons).toHaveLength(0)
  })
})

describe('getNotificationPermission', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns denied when Notification is unavailable', () => {
    vi.stubGlobal('Notification', undefined)
    expect(getNotificationPermission()).toBe('denied')
  })

  it('returns current permission', () => {
    vi.stubGlobal('Notification', { permission: 'granted' })
    expect(getNotificationPermission()).toBe('granted')
  })
})

describe('subscribePush / unsubscribePush / getPushSubscription', () => {
  const subscriptionJson = makeSubscription()
  const subscription = {
    toJSON: () => subscriptionJson,
    unsubscribe: vi.fn().mockResolvedValue(true),
  } as unknown as PushSubscription

  let pushManager: ReturnType<typeof mockPushManager>

  beforeEach(() => {
    clearPushSubscriptionCache()
    pushManager = mockPushManager(subscription)
    vi.stubGlobal('window', {
      isSecureContext: true,
      PushManager: class {},
    })
    vi.stubGlobal('navigator', {
      serviceWorker: {
        ready: Promise.resolve(mockServiceWorkerRegistration(pushManager)),
      },
    })
    vi.stubGlobal('Notification', {
      permission: 'granted',
      requestPermission: vi.fn().mockResolvedValue('granted'),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns existing subscription without re-subscribing', async () => {
    const result = await subscribePush('valid-key')
    expect(result).toEqual(subscriptionJson)
    expect(pushManager.subscribe).not.toHaveBeenCalled()
  })

  it('subscribes when no existing subscription', async () => {
    pushManager.getSubscription.mockResolvedValue(null)
    pushManager.subscribe.mockResolvedValue(subscription)
    const result = await subscribePush('aGVsbG8')
    expect(result).toEqual(subscriptionJson)
    expect(pushManager.subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: expect.any(Uint8Array),
    })
  })

  it('caches the subscribe promise across calls', async () => {
    const first = await subscribePush('key')
    const second = await subscribePush('key')
    expect(first).toBe(second)
    expect(pushManager.getSubscription).toHaveBeenCalledTimes(1)
  })

  it('throws permission-denied when permission already denied', async () => {
    vi.stubGlobal('Notification', { permission: 'denied' })
    await expect(subscribePush('key')).rejects.toMatchObject({ code: 'permission-denied' })
  })

  it('throws subscribe-failed on invalid VAPID key', async () => {
    pushManager.getSubscription.mockResolvedValue(null)
    await expect(subscribePush('!!!')).rejects.toMatchObject({ code: 'subscribe-failed' })
  })

  it('throws subscribe-failed when pushManager.subscribe rejects', async () => {
    pushManager.getSubscription.mockResolvedValue(null)
    pushManager.subscribe.mockRejectedValue(new Error('network'))
    await expect(subscribePush('aGVsbG8')).rejects.toMatchObject({ code: 'subscribe-failed' })
  })

  it('unsubscribes and clears cache', async () => {
    await subscribePush('key')
    expect(await unsubscribePush()).toBe(true)
    expect(subscription.unsubscribe).toHaveBeenCalled()
    expect(pushManager.getSubscription).toHaveBeenCalledTimes(2)
  })

  it('returns false when nothing to unsubscribe', async () => {
    pushManager.getSubscription.mockResolvedValue(null)
    expect(await unsubscribePush()).toBe(false)
  })

  it('getPushSubscription returns current subscription', async () => {
    expect(await getPushSubscription()).toEqual(subscriptionJson)
  })

  it('getPushSubscription returns null when unsupported', async () => {
    vi.stubGlobal('window', { isSecureContext: false })
    expect(await getPushSubscription()).toBeNull()
  })

  it('subscribePush throws PushSubscribeError with expected message', async () => {
    vi.stubGlobal('Notification', { permission: 'denied' })
    const promise = subscribePush('key')
    await expect(promise).rejects.toBeInstanceOf(PushSubscribeError)
    await expect(promise).rejects.toThrow('通知权限已被拒绝')
  })
})
