import { message } from '@repo/hooks'
import { HttpError, isHttpError } from '@repo/http-client'
import {
  abortUpload,
  declareUpload,
  finalizeUpload,
  getUploadStatus,
  uploadPart,
} from '@/api/upload'

/** 单分片大小:64 MiB(服务端上限,超过 declare 直接 422) */
const PART_SIZE = 64 * 1024 * 1024
/** 分片上传并发数(服务端建议 3-6 路) */
const PART_CONCURRENCY = 4
/** 单片失败串行退避重试次数(同一片禁止两路并发,重试必须串行) */
const PART_RETRY = 2
/** finalize 异步合并的轮询间隔(每次 poll 都是短请求,不触发代理 idle 超时) */
const FINALIZE_POLL_INTERVAL = 2_500

/** 会话级失效错误码:本地会话作废,重新 declare 全量重传(仅自动重建一次) */
const SESSION_INVALID_CODES = new Set(['session_not_found', 'session_expired', 'finalize_failed'])
/** 分片级不可重试错误码:会话失效/序号越界/配额问题,重传本片无意义 */
const PART_NO_RETRY_CODES = new Set([
  'session_not_found',
  'session_expired',
  'session_aborted',
  'session_finalizing',
  'part_out_of_range',
  'disk_full',
  'oversize',
])

/** 上传阶段:idle=空闲;uploading=分片上传中(sha 边传边算);finalizing=合并分片 */
export type UploadPhase = 'idle' | 'uploading' | 'finalizing'

export interface UploadState {
  phase: UploadPhase
  /** 0-100;按已传字节 / 总字节(含服务端已收片) */
  percentage: number
  /** 各文件上传进度(0-100,按文件名键控,清单行级展示;同名文件会共享进度) */
  filePercentages: Record<string, number>
}

/** 已建会话缓存:同一组文件重复探测时续传/复用,避免重复上传 */
interface UploadSession {
  /** 文件清单签名(name:size 排序拼接,变化则需重新 declare) */
  key: string
  sid: string
  stagedPath: string
  /** declare 按 files 数组顺序分配的文件索引(f0/f1/...),需自行维护 file_id ↔ File 映射 */
  fileIds: string[]
  finalized: boolean
}

/** 转十六进制字符串 */
function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** 软件哈希 worker(仅非安全上下文的降级路径使用,ensureUploaded 结束即销毁) */
let hashWorker: Worker | null = null
let hashRequestId = 0
const hashPending = new Map<
  number,
  { resolve: (hex: string) => void; reject: (error: Error) => void }
>()

function ensureHashWorker(): Worker {
  if (!hashWorker) {
    hashWorker = new Worker(new URL('./hash.worker.ts', import.meta.url), { type: 'module' })
    hashWorker.onmessage = (event: MessageEvent<{ id: number; hex?: string; error?: string }>) => {
      const { id, hex, error } = event.data
      const entry = hashPending.get(id)
      if (!entry) return
      hashPending.delete(id)
      if (error !== undefined) entry.reject(new Error(error))
      else entry.resolve(hex!)
    }
    hashWorker.onerror = (event) => {
      hashPending.forEach(({ reject }) => reject(new Error(event.message)))
      hashPending.clear()
    }
  }
  return hashWorker
}

function terminateHashWorker(): void {
  hashWorker?.terminate()
  hashWorker = null
  hashPending.clear()
}

/**
 * 计算 Blob 的 sha256(十六进制)。
 * 优先走原生 crypto.subtle(快);非安全上下文(http 内网部署)没有 subtle,
 * 降级为 @noble/hashes 纯 JS 实现并挪到 Web Worker,避免大文件哈希卡住 UI。
 */
async function sha256Hex(blob: Blob): Promise<string> {
  const buffer = await blob.arrayBuffer()
  if (crypto.subtle) {
    return toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', buffer)))
  }
  return new Promise<string>((resolve, reject) => {
    hashRequestId += 1
    hashPending.set(hashRequestId, { resolve, reject })
    // buffer 以 transfer 移交 worker,零拷贝
    ensureHashWorker().postMessage({ id: hashRequestId, buffer }, [buffer])
  })
}

/** 第 partNo 片(从 1 起)的实际字节数,末片可小于 PART_SIZE */
function partSizeAt(file: File, partNo: number): number {
  const offset = (partNo - 1) * PART_SIZE
  return Math.min(PART_SIZE, Math.max(file.size - offset, 0))
}

/**
 * 本地文件分片上传编排(/admin/uploads 通用切片上传协议):
 * declare 建会话(不带 sha,选中即开传)→ 查 status 拿 missing_parts →
 * 逐片切片 + 算 sha + 上传(边传边算,可并发,单片失败串行退避重试)→ finalize 触发异步合并 →
 * 轮询 status 至 completed → 幂等再调 finalize 取 staged_path。
 * 同一组文件重复调用走断点续传(只补缺失分片,服务端 received_parts 为权威),finalize 后直接复用路径。
 * 会话失效(session_not_found/session_expired/finalize_failed)自动重建一次;
 * 其余 API 失败抛错(提示由 http 中间件统一给出),调用方 catch 后中断即可。
 */
export function useChunkedUpload() {
  const state = reactive<UploadState>({ phase: 'idle', percentage: 0, filePercentages: {} })
  let session: UploadSession | null = null
  /** 各分片已传字节(key: fileIndex:partNo),驱动上传进度,重试不清零 */
  const uploadedByPart = new Map<string, number>()
  /** 各文件已传字节(key: fileIndex),由分片进度增量聚合,驱动行级进度 */
  const uploadedByFile = new Map<number, number>()
  /** 当前文件组元信息(fileIndex → name/size),行级进度按 name 暴露 */
  let fileMeta: { name: string; size: number }[] = []
  let totalBytes = 0
  /** 当前上传轮的取消控制器:abort() 中断在途分片请求并停掉任务队列 */
  let abortController: AbortController | null = null

  /** 已取消则抛错:中断任务队列/重试/finalize,错误由调用方静默捕获 */
  function throwIfAborted(): void {
    if (abortController?.signal.aborted) throw new DOMException('上传已取消', 'AbortError')
  }

  function updatePercentage(): void {
    const uploaded = [...uploadedByPart.values()].reduce((sum, loaded) => sum + loaded, 0)
    state.percentage = totalBytes ? Math.min(100, Math.round((uploaded / totalBytes) * 100)) : 100
    fileMeta.forEach((meta, fileIndex) => {
      const fileUploaded = uploadedByFile.get(fileIndex) ?? 0
      state.filePercentages[meta.name] = meta.size
        ? Math.min(100, Math.round((fileUploaded / meta.size) * 100))
        : 100
    })
  }

  /** 更新单个分片进度并同步聚合到文件级/总进度 */
  function setPartProgress(fileIndex: number, partNo: number, bytes: number): void {
    const partKey = `${fileIndex}:${partNo}`
    const prev = uploadedByPart.get(partKey) ?? 0
    uploadedByPart.set(partKey, bytes)
    uploadedByFile.set(fileIndex, (uploadedByFile.get(fileIndex) ?? 0) + bytes - prev)
    updatePercentage()
  }

  /** 清空进度记录(新会话/会话重建时调用) */
  function resetProgress(): void {
    uploadedByPart.clear()
    uploadedByFile.clear()
    state.filePercentages = {}
    state.percentage = 0
  }

  /** declare 建会话:只传文件清单(name/size/total_parts/part_size),瞬时返回 */
  async function createSession(
    files: File[],
    key: string,
    folderName: string | null,
  ): Promise<UploadSession> {
    state.phase = 'uploading'
    const declared = await declareUpload({
      // staged 目录名仅作磁盘子目录,优先取所选文件夹名,兜底首个文件名去扩展名
      name: folderName || files[0]?.name.replace(/\.[^.]+$/, '') || 'upload',
      files: files.map((file) => ({
        name: file.name,
        size: file.size,
        // 空文件也按 1 个分片声明(后端 total_parts >= 1)
        total_parts: Math.max(1, Math.ceil(file.size / PART_SIZE)),
        part_size: PART_SIZE,
      })),
    })
    return {
      key,
      sid: declared.sid,
      stagedPath: declared.staged_path,
      fileIds: declared.files.map((file) => file.file_id),
      finalized: false,
    }
  }

  /** 传单个分片:切片 → 算本片 sha256 → 字节 + sha 一起 POST;失败串行退避重试 */
  async function uploadPartWithRetry(
    current: UploadSession,
    fileIndex: number,
    partNo: number,
    file: File,
  ): Promise<void> {
    const offset = (partNo - 1) * PART_SIZE
    const blob = file.slice(offset, offset + PART_SIZE)
    const sha256 = await sha256Hex(blob)
    for (let attempt = 0; ; attempt += 1) {
      try {
        await uploadPart(
          current.sid,
          { file_id: current.fileIds[fileIndex], part_no: partNo, sha256, file: blob },
          (loaded) => setPartProgress(fileIndex, partNo, loaded),
          abortController?.signal,
        )
        setPartProgress(fileIndex, partNo, blob.size)
        return
      } catch (error) {
        // 取消优先:abort() 后在途请求被中断,直接抛错不再重试
        throwIfAborted()
        const noRetry =
          attempt >= PART_RETRY ||
          (isHttpError(error) && PART_NO_RETRY_CODES.has(String(error.code)))
        if (noRetry) throw error
        // 重试前清零本片已传进度,退避后串行重传(part_sha_mismatch/网络抖动可恢复)
        setPartProgress(fileIndex, partNo, 0)
        await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)))
      }
    }
  }

  /** 按会话状态补传缺失分片(断点续传:只传 missing_parts,不重传已收片) */
  async function uploadMissingParts(files: File[], current: UploadSession): Promise<void> {
    state.phase = 'uploading'
    const status = await getUploadStatus(current.sid)
    // 合并失败的会话无法续传:抛 finalize_failed 走会话作废重建(与轮询 failed 同语义)
    if (status.status === 'failed') {
      throw new HttpError({ type: 'http', message: '分片合并失败', code: 'finalize_failed' })
    }
    if (status.status === 'aborted') {
      message.error('上传会话已失效,请重新选择文件后重试')
      throw new Error('upload session aborted')
    }
    const tasks: { fileIndex: number; partNo: number }[] = []
    status.files.forEach((fileStatus, fileIndex) => {
      // 已收片字节计入进度,续传不从 0 起跳
      fileStatus.received_parts.forEach((partNo) => {
        setPartProgress(fileIndex, partNo, partSizeAt(files[fileIndex], partNo))
      })
      fileStatus.missing_parts.forEach((partNo) => tasks.push({ fileIndex, partNo }))
    })
    updatePercentage()
    let cursor = 0
    const worker = async (): Promise<void> => {
      while (cursor < tasks.length) {
        // abort() 后停止领取新任务,队列即刻排空
        throwIfAborted()
        const task = tasks[cursor]
        cursor += 1
        await uploadPartWithRetry(current, task.fileIndex, task.partNo, files[task.fileIndex])
      }
    }
    await Promise.all(
      Array.from({ length: Math.min(PART_CONCURRENCY, tasks.length) }, () => worker()),
    )
  }

  /** 轮询异步合并:finalizing 继续等;completed 返回;failed/aborted 视为会话失效,
   * 抛 finalize_failed 让 ensureUploaded 作废会话重新 declare 全传 */
  async function waitForFinalized(sid: string): Promise<void> {
    for (;;) {
      throwIfAborted()
      await new Promise((resolve) => setTimeout(resolve, FINALIZE_POLL_INTERVAL))
      throwIfAborted()
      const status = await getUploadStatus(sid)
      if (status.status === 'completed') return
      if (status.status !== 'finalizing' && status.status !== 'pending') {
        throw new HttpError({ type: 'http', message: '分片合并失败', code: 'finalize_failed' })
      }
    }
  }

  /** 建/复用会话 → 补传缺失分片 → finalize 合并,返回 staged 目录路径 */
  async function uploadOnce(files: File[], key: string, folderName: string | null): Promise<string> {
    if (!session || session.key !== key) {
      // 替换未完成的旧会话:尽力取消(已 finalize 的由后端清理),不阻塞新会话
      if (session && !session.finalized) abortUpload(session.sid).catch(() => {})
      session = null
      resetProgress()
      fileMeta = files.map((file) => ({ name: file.name, size: file.size }))
      totalBytes = files.reduce((sum, file) => sum + file.size, 0)
      session = await createSession(files, key, folderName)
    }
    if (!session.finalized) {
      await uploadMissingParts(files, session)
      // 补传完成后被 abort 的边界:不再发起 finalize
      throwIfAborted()
      state.phase = 'finalizing'
      // 1. 触发异步合并(文件没收齐仍同步 409 报错);合并本身后台跑,立即返回 finalizing
      await finalizeUpload(session.sid)
      // 2. 轮询直到 completed;failed 抛 finalize_failed → 会话作废重建全传
      await waitForFinalized(session.sid)
      // 3. completed 后幂等再调一次,拿 staged_path + 文件清单
      const { staged_path: stagedPath } = await finalizeUpload(session.sid)
      session.stagedPath = stagedPath ?? session.stagedPath
      session.finalized = true
    }
    state.phase = 'idle'
    state.percentage = 100
    return session.stagedPath
  }

  /** 上传一组文件并返回 staged 目录路径;同名同大小的文件组复用已有会话 */
  async function ensureUploaded(rawFiles: File[], folderName: string | null = null): Promise<string> {
    abortController = new AbortController()
    // 排序固定顺序:同一组文件选择顺序不同也命中会话缓存
    const files = [...rawFiles].sort((a, b) => a.name.localeCompare(b.name))
    const key = files.map((file) => `${file.name}:${file.size}`).join('\n')
    try {
      for (let attempt = 0; ; attempt += 1) {
        try {
          return await uploadOnce(files, key, folderName)
        } catch (error) {
          // 会话失效(不存在/过期/合并失败):服务端已清理,作废本地会话重新 declare 全量重传,仅自动重建一次
          const sessionInvalid =
            isHttpError(error) && SESSION_INVALID_CODES.has(String(error.code))
          if (attempt > 0 || !sessionInvalid) {
            // 轮询合成的 finalize_failed 不经过 http 中间件,重建仍失败时需自行提示
            if (sessionInvalid && isHttpError(error) && error.status === undefined) {
              message.error(error.message)
            }
            throw error
          }
          session = null
          resetProgress()
        }
      }
    } finally {
      // 软件降级哈希只在传片时用到,上传结束即销毁 worker
      terminateHashWorker()
    }
  }

  /** 取消上传(弹窗关闭/组件卸载时调用):中断在途分片请求 + 中止服务端会话 + 重置进度 */
  function abort(): void {
    abortController?.abort()
    if (session && !session.finalized) abortUpload(session.sid).catch(() => {})
    session = null
    resetProgress()
    state.phase = 'idle'
  }

  return { state, ensureUploaded, abort }
}
