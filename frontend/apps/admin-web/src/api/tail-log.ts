import { useAuthStore } from '@/store/auth'

/**
 * 容器 stdout 追踪(POST /admin/orchestration/nodes/{node_id}/tasks/tail-log)。
 * 全仓唯一不走 Result 包裹的接口:Content-Type: application/x-ndjson,
 * 每行一个 JSON 帧,故绕过 axios 拦截器用原生 fetch 读流。
 */

/** NDJSON 帧:data=一行日志原文 / error=建流后出错 / end=正常结束 / ping=服务端心跳(忽略) */
export type TailLogFrame =
  | { event: 'data'; data: string }
  | { event: 'error'; message: string }
  | { event: 'end' }
  | { event: 'ping' }

export interface TailLogOptions {
  /** 目标节点(实例 assigned_node) */
  nodeId: string
  containerName: string
  /** 初始返回的尾部日志行数 */
  lines: number
  /** 调用方持有 AbortController;开新流/关闭面板前 abort 旧的(全局单流) */
  signal: AbortSignal
  onFrame: (frame: TailLogFrame) => void
}

/** 拼请求地址:与 request.ts 同源(baseURL 可能带尾斜杠,统一去掉再拼) */
function tailLogUrl(nodeId: string): string {
  const base = (import.meta.env.VITE_ADMIN_API_BASE_URL ?? '').replace(/\/+$/, '')
  return `${base}/admin/orchestration/nodes/${nodeId}/tasks/tail-log`
}

/** 逐行解析 NDJSON 流;帧 JSON 非法(如截断)静默丢弃,不让整条流中断 */
export async function tailContainerLog(options: TailLogOptions): Promise<void> {
  const { nodeId, containerName, lines, signal, onFrame } = options
  const response = await fetch(tailLogUrl(nodeId), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${useAuthStore().token ?? ''}`,
    },
    body: JSON.stringify({ container_name: containerName, lines }),
    signal,
  })

  // 非 200:统一 Result 错误 JSON(node 离线等),读 message 抛出
  if (!response.ok || !response.body) {
    let reason = `HTTP ${response.status}`
    try {
      const payload = (await response.json()) as { message?: string }
      if (payload.message) reason = payload.message
    } catch {
      // 非 JSON 错误体,保留 HTTP 状态文案
    }
    throw new Error(reason)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // 按行切帧;最后一段可能是不完整行,留在 buffer 等下一批
    const segments = buffer.split('\n')
    buffer = segments.pop() ?? ''
    for (const segment of segments) {
      const line = segment.trim()
      if (!line) continue
      try {
        const frame = JSON.parse(line) as TailLogFrame
        // 服务端心跳帧:不透传给调用方,避免被误判为流结束
        if (frame.event !== 'ping') onFrame(frame)
      } catch {
        // 半行/非 JSON 行:丢弃
      }
    }
  }
  // 收尾:解析残留 buffer(流以无换行结束时)
  const rest = buffer.trim()
  if (rest) {
    try {
      const frame = JSON.parse(rest) as TailLogFrame
      if (frame.event !== 'ping') onFrame(frame)
    } catch {
      // 丢弃
    }
  }
}
