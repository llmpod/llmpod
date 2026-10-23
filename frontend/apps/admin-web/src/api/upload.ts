import { request } from './request'

/** 单文件上传声明:目标名 + 总大小 + 分片数(declare 不传 sha,sha 随每个 part 上传时自带) */
export interface DeclareFileSpec {
  /** 目标文件名(合并后的最终名,含扩展名) */
  name: string
  /** 文件总大小(字节) */
  size: number
  /** 分片总数(⌈size / part_size⌉,空文件按 1 计) */
  total_parts: number
  /** 单分片大小(字节,末片可小于;最大 64 MiB,超过 declare 直接 422) */
  part_size: number
}

/** 建上传会话请求体(POST /api/v1/admin/uploads/declare) */
export interface DeclareUploadPayload {
  /** staged 目录名(清洗后用作磁盘子目录,防穿越) */
  name: string
  /** 本会话要上传的文件清单 */
  files: DeclareFileSpec[]
}

/** declare 返回的单文件元信息 */
export interface DeclareFileAck {
  /** server 分配的文件顺序索引(f0/f1/...),part 按此定位 */
  file_id: string
  /** 目标文件名(回显) */
  name: string
}

/** declare 成功响应 */
export interface DeclareUploadResult {
  /** 会话 ID(不可猜,后续 part/status/finalize/abort 用) */
  sid: string
  /** staged 目录绝对路径,finalize 后拿去 inspect */
  staged_path: string
  /** 会话状态:pending */
  status: string
  /** 各文件 file_id + 目标名清单 */
  files: DeclareFileAck[]
}

/** 建上传会话(POST /api/v1/admin/uploads/declare):通用预校验 + 磁盘预检 */
export function declareUpload(data: DeclareUploadPayload): Promise<DeclareUploadResult> {
  return request({ url: '/admin/uploads/declare', method: 'POST', data })
}

/** part 上传成功响应 */
export interface UploadPartResult {
  file_id: string
  part_no: number
  /** 是否已收(幂等:已在也返回 true) */
  received: boolean
  /** 会话当前状态(pending/finalizing/...) */
  session_status: string
}

/** 传分片(POST /api/v1/admin/uploads/{sid}/part,multipart;sha 校验 + 原子落盘,幂等);signal 用于取消在途请求 */
export function uploadPart(
  sid: string,
  part: { file_id: string; part_no: number; sha256: string; file: Blob },
  onProgress?: (loaded: number) => void,
  signal?: AbortSignal,
): Promise<UploadPartResult> {
  const data = new FormData()
  data.append('file_id', part.file_id)
  data.append('part_no', String(part.part_no))
  data.append('sha256', part.sha256)
  data.append('file', part.file)
  return request({
    url: `/admin/uploads/${sid}/part`,
    method: 'POST',
    data,
    // 与 uploadModel 同理:置 null 删除默认 json 头,由浏览器自动按 multipart 补 boundary;
    // 单分片较大,放宽默认 30s 超时
    headers: { 'Content-Type': null },
    timeout: 10 * 60_000,
    signal,
    onUploadProgress: (event) => onProgress?.(event.loaded),
  })
}

/** 单文件的断点续传状态 */
export interface UploadFileStatus {
  /** 文件索引 */
  file_id: string
  /** 目标文件名 */
  name: string
  /** 分片总数 */
  total_parts: number
  /** 已收分片序号(升序) */
  received_parts: number[]
  /** 缺失分片序号(断点续传只补传这些) */
  missing_parts: number[]
  /** 是否已收齐 */
  complete: boolean
}

/** 会话状态查询响应 */
export interface UploadStatusResult {
  sid: string
  /** 会话状态:pending/finalizing/completed/aborted/failed */
  status: string
  files: UploadFileStatus[]
}

/** 查会话状态(GET /api/v1/admin/uploads/{sid}):断点续传依据;也是 finalize 合并进度的轮询接口,短请求 5s 超时 */
export function getUploadStatus(sid: string): Promise<UploadStatusResult> {
  return request({ url: `/admin/uploads/${sid}`, method: 'GET', timeout: 5_000 })
}

/** finalize 返回的单文件合并结果 */
export interface FinalizeFileAck {
  file_id: string
  /** 合并后的目标文件名 */
  name: string
  /** 合并后文件大小(字节) */
  size: number
}

/** finalize 响应:首次调用返回 finalizing(合并后台跑);completed 后再调幂等返回 staged_path + 文件清单 */
export interface FinalizeUploadResult {
  sid: string
  /** staged 目录绝对路径,仅 status=completed 时返回,拿去 inspect */
  staged_path?: string
  /** 会话状态:finalizing=合并进行中;completed=合并完成 */
  status: string
  /** 合并后文件清单(finalizing 时为空数组) */
  files: FinalizeFileAck[]
}

/** 合并分片(POST /api/v1/admin/uploads/{sid}/finalize):全文件收齐才合并,否则同步 409;合并本身异步,
 * 只做校验 + 起后台任务(几次 Redis 往返),5s 超时足够;幂等 */
export function finalizeUpload(sid: string): Promise<FinalizeUploadResult> {
  return request({ url: `/admin/uploads/${sid}/finalize`, method: 'POST', timeout: 5_000 })
}

/** 取消会话(POST /api/v1/admin/uploads/{sid}/abort):删盘 + 删 Redis + 墓碑;幂等,仅 pending 可 abort */
export function abortUpload(sid: string): Promise<{ sid: string; status: string }> {
  return request({ url: `/admin/uploads/${sid}/abort`, method: 'POST' })
}
