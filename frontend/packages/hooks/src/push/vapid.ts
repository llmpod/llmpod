const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/**
 * 将 base64url 编码的 VAPID 公钥转换为 Uint8Array,供 pushManager.subscribe 使用。
 * 非法输入返回 null,由调用方决定降级或报错。
 */
export function urlBase64ToUint8Array(base64String: string): Uint8Array | null {
  if (!base64String) return null
  try {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
    for (const char of base64) {
      if (!BASE64_CHARS.includes(char) && char !== '=') return null
    }
    const rawData = atob(base64)
    const outputArray = new Uint8Array(rawData.length)
    for (let index = 0; index < rawData.length; index += 1) {
      outputArray[index] = rawData.charCodeAt(index)
    }
    return outputArray
  }
  catch {
    return null
  }
}
