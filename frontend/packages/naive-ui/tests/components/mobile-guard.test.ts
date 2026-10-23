// @vitest-environment happy-dom
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/** 读包内源文件内容(相对本测试文件定位) */
function readSrc(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8')
}

describe('窄屏(≤767px)全局保护样式', () => {
  it('LmDialog 窄屏宽度钳制存在(calc(100vw - 2rem))', () => {
    const source = readSrc('../../src/components/dialog/LmDialog.vue')
    expect(source).toContain('@media (max-width: 767px)')
    expect(source).toContain('calc(100vw - 2rem)')
  })

  it('global.scss 含抽屉钳宽(92vw)与描述列表标签退化(width auto)', () => {
    const css = readSrc('../../src/styles/global.scss')
    expect(css).toContain('@media (max-width: 767px)')
    expect(css).toContain('.n-drawer')
    expect(css).toContain('92vw')
    expect(css).toContain('.n-descriptions-table-header')
    expect(css).toContain('width: auto')
  })
})
