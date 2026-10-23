// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import {
  ADMIN_TOKEN_KEY,
  LAST_PLATFORM_KEY,
  TENANT_TOKEN_KEY,
  clearAllTokens,
  clearToken,
  getLastPlatform,
  getToken,
  setToken,
} from '../src/auth'
import { getActivePlatform } from '../src/auth/platform'
import { buildPlatformUrl, buildPortalLoginUrl } from '../src/auth/navigate'

describe('auth tokens', () => {
  afterEach(() => localStorage.clear())

  it('exposes stable token key constants', () => {
    expect(TENANT_TOKEN_KEY).toBe('tenant_access_token')
    expect(ADMIN_TOKEN_KEY).toBe('admin_access_token')
    expect(LAST_PLATFORM_KEY).toBe('llmpod:last-platform')
  })

  it('writes and reads token per platform', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    expect(getToken('tenant')).toBe('t1')
    expect(getToken('admin')).toBe('a1')
    expect(localStorage.getItem(TENANT_TOKEN_KEY)).toBe('t1')
    expect(localStorage.getItem(ADMIN_TOKEN_KEY)).toBe('a1')
  })

  it('returns empty string when token missing', () => {
    expect(getToken('tenant')).toBe('')
  })

  it('clears a single platform token', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    clearToken('tenant')
    expect(getToken('tenant')).toBe('')
    expect(getToken('admin')).toBe('a1')
  })

  it('clears all tokens', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    clearAllTokens()
    expect(getToken('tenant')).toBe('')
    expect(getToken('admin')).toBe('')
  })

  it('tracks the most recent login platform on setToken', () => {
    setToken('tenant', 't1')
    expect(getLastPlatform()).toBe('tenant')
    setToken('admin', 'a1')
    expect(getLastPlatform()).toBe('admin')
    expect(localStorage.getItem(LAST_PLATFORM_KEY)).toBe('admin')
  })

  it('clears the recent-platform mark only when clearing that platform', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    clearToken('tenant')
    expect(getLastPlatform()).toBe('admin')
    clearToken('admin')
    expect(getLastPlatform()).toBeNull()
  })

  it('clearAllTokens also clears the recent-platform mark', () => {
    setToken('admin', 'a1')
    clearAllTokens()
    expect(getLastPlatform()).toBeNull()
  })

  it('returns null for an invalid recent-platform mark value', () => {
    localStorage.setItem(LAST_PLATFORM_KEY, 'foo')
    expect(getLastPlatform()).toBeNull()
  })
})

describe('getActivePlatform', () => {
  afterEach(() => localStorage.clear())

  it('returns null when no token', () => {
    expect(getActivePlatform()).toBeNull()
  })

  it('returns the platform matching its token', () => {
    setToken('tenant', 't1')
    expect(getActivePlatform()).toBe('tenant')
    localStorage.clear()
    setToken('admin', 'a1')
    expect(getActivePlatform()).toBe('admin')
  })

  it('prefers the most recent login platform when both tokens exist', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    expect(getActivePlatform()).toBe('admin')
  })

  it('falls back to the remaining token after the recent platform logs out', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    clearToken('admin')
    expect(getActivePlatform()).toBe('tenant')
  })

  it('falls back to token probing when the recent-platform mark is stale', () => {
    setToken('tenant', 't1')
    setToken('admin', 'a1')
    // 绕过 clearToken 直接删 token,制造标记残留(如旧版本数据)
    localStorage.removeItem(ADMIN_TOKEN_KEY)
    expect(getActivePlatform()).toBe('tenant')
  })

  it('prefers tenant when both tokens exist without any mark (legacy residue)', () => {
    // 直接写 key 不经过 setToken,模拟无标记的历史数据
    localStorage.setItem(TENANT_TOKEN_KEY, 't1')
    localStorage.setItem(ADMIN_TOKEN_KEY, 'a1')
    expect(getActivePlatform()).toBe('tenant')
  })
})

describe('navigate url builders', () => {
  it('buildPlatformUrl composes platform base + path', () => {
    expect(buildPlatformUrl('tenant')).toBe('/tenant/dashboard')
    expect(buildPlatformUrl('admin', '/users')).toBe('/admin/users')
  })

  it('buildPortalLoginUrl returns the portal login url', () => {
    expect(buildPortalLoginUrl('http://localhost:5175')).toBe('http://localhost:5175/login')
  })
})
