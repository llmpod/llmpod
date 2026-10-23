// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DEFAULT_BRAND_CONFIG, useBrand } from '../src'

describe('useBrand 品牌配置', () => {
  afterEach(() => localStorage.clear())

  it('默认配置为 DEFAULT_BRAND_CONFIG', () => {
    const { brandConfig } = useBrand()
    expect(brandConfig.value.footerText).toBe(DEFAULT_BRAND_CONFIG.footerText)
  })

  it('setBrandConfig 合并更新并持久化到 localStorage', async () => {
    const { brandConfig, setBrandConfig } = useBrand()
    setBrandConfig({ footerText: '© 2026 新品牌' })

    expect(brandConfig.value.footerText).toBe('© 2026 新品牌')
    // useStorage 经 watch 异步落盘,需等一拍
    await nextTick()
    const stored = JSON.parse(localStorage.getItem('app_brand') ?? '{}') as { footerText?: string }
    expect(stored.footerText).toBe('© 2026 新品牌')

    // 还原默认值避免污染同文件其它用例(共享单例)
    setBrandConfig({ footerText: DEFAULT_BRAND_CONFIG.footerText })
    await nextTick()
  })

  it('初始化读取 localStorage 已有值(隔离模块重验证)', async () => {
    const { vi } = await import('vitest')
    vi.resetModules()
    localStorage.setItem('app_brand', JSON.stringify({ footerText: '© 2026 持久化' }))
    const fresh = await import('../src')
    expect(fresh.useBrand().brandConfig.value.footerText).toBe('© 2026 持久化')
  })
})

describe('useBrand favicon 联动', () => {
  const { setBrandConfig } = useBrand()

  /** 模拟三端 index.html 的静态 favicon link */
  beforeEach(() => {
    document.head.innerHTML = '<link rel="icon" type="image/x-icon" href="/logo.ico">'
  })
  afterEach(() => setBrandConfig({ logo: '' }))

  function faviconLink(): HTMLLinkElement {
    return document.querySelector('link[rel="icon"]')!
  }

  it('emoji logo → 转 SVG data URI favicon', async () => {
    setBrandConfig({ logo: '🚀' })
    await nextTick()

    const link = faviconLink()
    expect(link.getAttribute('href')).toMatch(/^data:image\/svg\+xml,/)
    expect(decodeURIComponent(link.getAttribute('href')!)).toContain('🚀')
    expect(link.type).toBe('image/svg+xml')
  })

  it('图片 URL logo → 直挂 favicon 并移除 type(交浏览器嗅探)', async () => {
    setBrandConfig({ logo: 'https://cdn.example.com/logo.png' })
    await nextTick()

    const link = faviconLink()
    expect(link.getAttribute('href')).toBe('https://cdn.example.com/logo.png')
    expect(link.getAttribute('type')).toBeNull()
  })

  it('data:image logo → 直挂并按前缀推断 MIME', async () => {
    setBrandConfig({ logo: 'data:image/png;base64,QUJD' })
    await nextTick()

    const link = faviconLink()
    expect(link.getAttribute('href')).toBe('data:image/png;base64,QUJD')
    expect(link.type).toBe('image/png')
  })

  it('清空 logo → 回落 /logo.ico', async () => {
    setBrandConfig({ logo: 'https://cdn.example.com/logo.png' })
    await nextTick()
    setBrandConfig({ logo: '' })
    await nextTick()

    const link = faviconLink()
    expect(link.getAttribute('href')!.endsWith('/logo.ico')).toBe(true)
    expect(link.type).toBe('image/x-icon')
  })
})
