import { request } from './request'

/**
 * NPS 反馈请求体。
 * 只有评分与评语两个字段，其余（平台域、提交人、时间等）全部由服务端写入。
 */
export interface NpsSubmitPayload {
  /** 评分 1–5，对应弹窗星级；越界后端返回 422 */
  rate: number
  /** 一句话评语，最长 500 字；空评语传 null */
  comment: string | null
}

/**
 * 提交 NPS 满意度反馈（POST /api/v1/admin/nps）。
 *
 * 平台域路由，带管理端登录态即可，无需额外权限（tenant/portal 侧不可用）。
 * 成功返回 null；失败时 400 的 message 已含中文原因（如「反馈提交失败：错误的参数【SERVER_ID】」），
 * 由 app 的 request 实例统一 toast，这里不吞异常——继续抛给弹窗，
 * 让弹窗保持打开并提示可重试（不自动重试，用户可再点提交）。
 */
export function submitNps(payload: NpsSubmitPayload): Promise<null> {
  return request({ url: '/admin/nps', method: 'POST', data: payload })
}
