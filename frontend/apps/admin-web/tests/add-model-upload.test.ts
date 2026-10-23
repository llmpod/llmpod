import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/upload', () => ({
  declareUpload: vi.fn(),
  finalizeUpload: vi.fn(),
  getUploadStatus: vi.fn(),
  uploadPart: vi.fn(),
  abortUpload: vi.fn(),
}))
vi.mock('@repo/hooks', () => ({ message: { error: vi.fn(), warning: vi.fn(), success: vi.fn() } }))

import {
  abortUpload,
  declareUpload,
  finalizeUpload,
  getUploadStatus,
  uploadPart,
} from '@/api/upload'
import { HttpError } from '@repo/http-client'
import { useChunkedUpload } from '@/views/model-management/add-model/useUpload'

const PART_SIZE = 64 * 1024 * 1024
/** finalize 轮询间隔 2500ms(真实定时器:crypto.subtle 与 Blob IO 底层异步同 fake timers 不兼容) */
const POLL_MS = 2600

/** Node File 构造不支持自定义 size,用 defineProperty 覆盖 */
function fileOf(name: string, size: number): File {
  const file = new File([new Uint8Array(8)], name)
  Object.defineProperty(file, 'size', { value: size })
  return file
}

function httpError(code: string): HttpError {
  return new HttpError({ type: 'http', message: code, code })
}

type UploadStatus = Awaited<ReturnType<typeof getUploadStatus>>

/** 标准 mock:declare + 单次补传 + finalize + 轮询 completed */
function mockHappyPath(missingParts: number[] = [1]) {
  vi.mocked(declareUpload).mockResolvedValue({
    sid: 'sid-1',
    staged_path: '/staged/a',
    files: [{ file_id: 'f0' }],
  } as Awaited<ReturnType<typeof declareUpload>>)
  vi.mocked(getUploadStatus)
    .mockResolvedValueOnce({
      status: 'uploading',
      files: [{ received_parts: [], missing_parts: missingParts }],
    } as UploadStatus)
    .mockResolvedValue({ status: 'completed', files: [] } as UploadStatus)
  vi.mocked(uploadPart).mockImplementation(async (_sid, _part, onProgress) => {
    onProgress?.(PART_SIZE)
  })
  vi.mocked(finalizeUpload).mockResolvedValue({ staged_path: '/staged/a' } as Awaited<
    ReturnType<typeof finalizeUpload>
  >)
  vi.mocked(abortUpload).mockResolvedValue(undefined as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockHappyPath()
})

describe('useChunkedUpload 分片上传状态机', () => {
  it('完整链路:declare→补传缺失片→finalize→轮询→取 staged_path', async () => {
    const upload = useChunkedUpload()
    const staged = await upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], 'Qwen2.5-7B')

    expect(declareUpload).toHaveBeenCalledTimes(1)
    expect(declareUpload.mock.calls[0][0]).toMatchObject({ name: 'Qwen2.5-7B' })
    expect(uploadPart).toHaveBeenCalledTimes(1)
    expect(uploadPart.mock.calls[0][1]).toMatchObject({ file_id: 'f0', part_no: 1 })
    expect(finalizeUpload).toHaveBeenCalledTimes(2) // 触发合并 + completed 后幂等取路径
    expect(staged).toBe('/staged/a')
    expect(upload.state.percentage).toBe(100)
    expect(upload.state.phase).toBe('idle')
  }, POLL_MS * 2)

  it('空文件按 1 个分片声明(total_parts >= 1)', async () => {
    const upload = useChunkedUpload()
    await upload.ensureUploaded([fileOf('empty.bin', 0)], null)
    expect(declareUpload.mock.calls[0][0].files[0]).toMatchObject({ total_parts: 1, size: 0 })
  }, POLL_MS * 2)

  it('已 finalize 的同组文件直接复用路径,不重新 declare 不传片', async () => {
    const upload = useChunkedUpload()
    const files = [fileOf('model.safetensors', PART_SIZE)]
    await upload.ensureUploaded(files, null)
    expect(declareUpload).toHaveBeenCalledTimes(1)

    const again = await upload.ensureUploaded(files, null)
    expect(again).toBe('/staged/a')
    expect(declareUpload).toHaveBeenCalledTimes(1)
    expect(getUploadStatus).toHaveBeenCalledTimes(2) // 仅首轮 missing 检查 + 轮询
  }, POLL_MS * 2)

  it('断点续传:服务端已收片计入进度,只补 missing_parts', async () => {
    const upload = useChunkedUpload()
    // 两片文件,服务端已收第 1 片
    vi.mocked(getUploadStatus)
      .mockReset()
      .mockResolvedValueOnce({
        status: 'uploading',
        files: [{ received_parts: [1], missing_parts: [2] }],
      } as UploadStatus)
      .mockResolvedValue({ status: 'completed', files: [] } as UploadStatus)

    await upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE * 2)], null)

    expect(uploadPart).toHaveBeenCalledTimes(1)
    expect(uploadPart.mock.calls[0][1]).toMatchObject({ part_no: 2 }) // 只补第 2 片
  }, POLL_MS * 2)

  it('会话失效(session_not_found)自动重建一次并重传', async () => {
    const upload = useChunkedUpload()
    vi.mocked(uploadPart)
      .mockRejectedValueOnce(httpError('session_not_found'))
      .mockImplementation(async (_sid, _part, onProgress) => onProgress?.(PART_SIZE))
    // 首次 + 重建各一轮 missing 检查,随后轮询 completed
    vi.mocked(getUploadStatus)
      .mockReset()
      .mockResolvedValueOnce({ status: 'uploading', files: [{ received_parts: [], missing_parts: [1] }] } as UploadStatus)
      .mockResolvedValueOnce({ status: 'uploading', files: [{ received_parts: [], missing_parts: [1] }] } as UploadStatus)
      .mockResolvedValue({ status: 'completed', files: [] } as UploadStatus)

    await expect(upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], null)).resolves.toBe('/staged/a')
    expect(declareUpload).toHaveBeenCalledTimes(2)
  }, POLL_MS * 3)

  it('会话连续失效:重建后仍失败则抛错(仅自动重建一次)', async () => {
    const upload = useChunkedUpload()
    vi.mocked(uploadPart).mockRejectedValue(httpError('session_not_found'))
    vi.mocked(getUploadStatus)
      .mockReset()
      .mockResolvedValue({ status: 'uploading', files: [{ received_parts: [], missing_parts: [1] }] } as UploadStatus)

    await expect(upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], null)).rejects.toMatchObject({ code: 'session_not_found' })
    expect(declareUpload).toHaveBeenCalledTimes(2) // 首次 + 仅一次重建
  })

  it('分片不可重试错误码(part_out_of_range)直接抛', async () => {
    const upload = useChunkedUpload()
    vi.mocked(uploadPart).mockRejectedValue(httpError('part_out_of_range'))
    await expect(upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], null)).rejects.toMatchObject({ code: 'part_out_of_range' })
    expect(uploadPart).toHaveBeenCalledTimes(1)
  })

  it('普通错误串行重试至多 PART_RETRY 次(退避 1s+2s)后抛错', async () => {
    const upload = useChunkedUpload()
    vi.mocked(uploadPart).mockRejectedValue(new Error('network'))
    await expect(upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], null)).rejects.toThrow('network')
    expect(uploadPart).toHaveBeenCalledTimes(3) // 首试 + 2 次重试
  }, 10_000)

  it('abort:中断上传,abortUpload 调用,进度与阶段复位', async () => {
    const upload = useChunkedUpload()
    vi.mocked(uploadPart).mockImplementation(async () => new Promise(() => {}))
    const pending = upload.ensureUploaded([fileOf('model.safetensors', PART_SIZE)], null)
    pending.catch(() => {})

    await Promise.resolve()
    await Promise.resolve()
    expect(upload.state.phase).toBe('uploading')

    upload.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(abortUpload).toHaveBeenCalledWith('sid-1')
    expect(upload.state.phase).toBe('idle')
    expect(upload.state.percentage).toBe(0)
  })
})
