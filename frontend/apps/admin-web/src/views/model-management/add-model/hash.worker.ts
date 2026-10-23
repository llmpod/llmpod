import { sha256 } from '@noble/hashes/sha2.js'

/** 主线程消息:计算 buffer 的 sha256 */
interface HashRequest {
  id: number
  buffer: ArrayBuffer
}

/** 回包:成功带 hex,失败带 error */
interface HashResponse {
  id: number
  hex?: string
  error?: string
}

// Vite 以 module worker 构建;self 即 worker 全局,这里收窄到用到的两个成员
const scope = self as unknown as {
  onmessage: ((event: MessageEvent<HashRequest>) => void) | null
  postMessage: (message: HashResponse) => void
}

/**
 * 软件 sha256 worker:仅在非安全上下文(http 内网部署,无 crypto.subtle)时启用,
 * 把 @noble/hashes 的纯 JS 计算移出主线程,避免 GB 级模型文件哈希卡住 UI。
 */
scope.onmessage = (event) => {
  const { id, buffer } = event.data
  try {
    const digest = sha256(new Uint8Array(buffer))
    const hex = Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
    scope.postMessage({ id, hex })
  } catch (error) {
    scope.postMessage({ id, error: String(error) })
  }
}
