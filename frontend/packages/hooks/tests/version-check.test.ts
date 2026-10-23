// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeAllMessage, useVersionCheck, type VersionCheckResult } from '../src'
import { mountWithHost } from './helpers/mount'

const STORAGE_KEY = 'llmpod:tenant:update-viewed'
const CURRENT = '26.9.3'
const LATEST = '26.9.4'

let wrapper: ReturnType<typeof mountWithHost> | null = null

/** 构造 /version/latest 响应 data；默认无更新、检查启用 */
function makeResult(overrides: Partial<VersionCheckResult> = {}): VersionCheckResult {
  return {
    current_version: CURRENT,
    latest_version: CURRENT,
    has_update: false,
    update_check_enabled: true,
    checked_at: null,
    notes: null,
    release_url: null,
    published_at: null,
    error_message: null,
    ...overrides,
  }
}

/** 建一个 hook 实例，fetchLatest 固定返回给定结果 */
function setup(result: VersionCheckResult, storageKey = STORAGE_KEY) {
  return useVersionCheck({ storageKey, fetchLatest: () => Promise.resolve(result) })
}

beforeEach(() => {
  wrapper = mountWithHost(() => h('div'))
})

afterEach(() => {
  closeAllMessage()
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  localStorage.clear()
})

describe('useVersionCheck 版本更新检查', () => {
  it('静默检查:has_update 为 true 且未查看 → 红点;has_update 为 false(含禁用/上游失败) → 无红点', async () => {
    const withUpdate = setup(makeResult({ latest_version: LATEST, has_update: true }))
    await withUpdate.silentCheck()
    expect(withUpdate.hasUpdate.value).toBe(true)
    // 上游报告有更新 → 更新内容展示信号恒 true
    expect(withUpdate.hasNewVersion.value).toBe(true)
    // 静默检查不弹提示
    expect(document.body.textContent).not.toContain('新版本')

    const noUpdate = setup(makeResult(), 'llmpod:test:key2')
    await noUpdate.silentCheck()
    expect(noUpdate.hasUpdate.value).toBe(false)
    expect(noUpdate.hasNewVersion.value).toBe(false)

    // 检查禁用:latest == current、has_update 为 false → 无红点
    const disabled = setup(makeResult({ update_check_enabled: false }), 'llmpod:test:key3')
    await disabled.silentCheck()
    expect(disabled.hasUpdate.value).toBe(false)
    expect(disabled.hasNewVersion.value).toBe(false)

    // 上游失败:error_message 非空、has_update 为 false → 无红点
    const upstreamError = setup(
      makeResult({ has_update: false, error_message: '更新源暂不可用' }),
      'llmpod:test:key4',
    )
    await upstreamError.silentCheck()
    expect(upstreamError.hasUpdate.value).toBe(false)
    expect(upstreamError.hasNewVersion.value).toBe(false)
  })

  it('静默检查吞错:接口 reject 不抛出、不弹提示', async () => {
    const failing = useVersionCheck({
      storageKey: 'llmpod:test:key5',
      fetchLatest: () => Promise.reject(new Error('network')),
    })
    await expect(failing.silentCheck()).resolves.toBeUndefined()
    expect(failing.hasUpdate.value).toBe(false)
    expect(document.body.textContent).not.toContain('检查更新失败')
  })

  it('手动检查有新版本:提示发现新版本,标记已查看后红点消失并持久化,返回检查结果', async () => {
    const { hasUpdate, hasNewVersion, manualCheck } = setup(
      makeResult({ latest_version: LATEST, has_update: true }),
    )

    const result = await manualCheck()
    await nextTick()
    expect(result?.has_update).toBe(true)
    expect(document.body.textContent).toContain('发现新版本 v26.9.4')
    expect(hasUpdate.value).toBe(false)
    // 红点已消但更新内容展示信号不受影响
    expect(hasNewVersion.value).toBe(true)

    // useStorage 经 watch 异步落盘,需等一拍
    await nextTick()
    expect(localStorage.getItem(STORAGE_KEY)).toBe(LATEST)
  })

  it('手动检查无新版本(含禁用):提示当前为最新版本', async () => {
    const { hasUpdate, manualCheck } = setup(makeResult())

    await manualCheck()
    await nextTick()
    expect(document.body.textContent).toContain('当前为最新版本')
    expect(hasUpdate.value).toBe(false)

    const disabled = setup(makeResult({ update_check_enabled: false }), 'llmpod:test:key6')
    await disabled.manualCheck()
    await nextTick()
    expect(document.body.textContent).toContain('当前为最新版本')
  })

  it('手动检查上游失败(error_message 非空):轻提示错误文案,不提示最新版本', async () => {
    const { hasUpdate, manualCheck } = setup(makeResult({ error_message: '更新源暂不可用' }))

    await manualCheck()
    await nextTick()
    expect(document.body.textContent).toContain('更新源暂不可用')
    expect(document.body.textContent).not.toContain('当前为最新版本')
    expect(hasUpdate.value).toBe(false)
  })

  it('手动检查网络失败:提示稍后重试,不标记已查看,返回 null', async () => {
    const failing = useVersionCheck({
      storageKey: STORAGE_KEY,
      fetchLatest: () => Promise.reject(new Error('network')),
    })
    await expect(failing.manualCheck()).resolves.toBeNull()
    await nextTick()
    expect(document.body.textContent).toContain('检查更新失败')
    // useStorage 初始化即落默认空串;断言未写入任何版本号即视为未标记已查看
    expect(localStorage.getItem(STORAGE_KEY)).not.toBe(LATEST)
  })

  it('查看过新版本后再次检查:红点不再出现,但手动检查仍提示发现新版本且展示信号恒 true', async () => {
    const first = setup(makeResult({ latest_version: LATEST, has_update: true }))
    await first.manualCheck()
    expect(first.hasUpdate.value).toBe(false)
    expect(first.hasNewVersion.value).toBe(true)

    // 同 storageKey 新实例(模拟刷新后):已查看版本被读取,红点不出现
    const second = setup(makeResult({ latest_version: LATEST, has_update: true }))
    await second.silentCheck()
    expect(second.hasUpdate.value).toBe(false)
    // 上游仍报告有更新 → 关于页更新内容照常展示
    expect(second.hasNewVersion.value).toBe(true)

    closeAllMessage()
    await nextTick()
    const result = await second.manualCheck()
    await nextTick()
    expect(result?.has_update).toBe(true)
    expect(document.body.textContent).toContain('发现新版本 v26.9.4')
    expect(second.hasNewVersion.value).toBe(true)
  })
})
