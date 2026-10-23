import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const dir = resolve(dirname(fileURLToPath(import.meta.url)), '../public/animations')

function load(name: string) {
  return JSON.parse(readFileSync(resolve(dir, name), 'utf8'))
}

interface Kf {
  t: number
  s?: number[]
}
interface Prop {
  a?: number
  k?: unknown
}

/**
 * 循环收口校验:首尾值相等即闭合。两类「值不等但视觉无缝」的合法例外单独放行:
 * - 旋转走满 360° 的整数倍(360°≡0°,末帧与首帧同位置);
 * - 虚线 offset 走满虚线周期(dash+gap)的整数倍(虚线相位相同)。
 * 另行:flow:* 巡游高光两端均不可见,由专门用例覆盖,此处跳过。
 */
function assertLoops(anim: Record<string, unknown>) {
  const layers = (anim.layers as Array<Record<string, unknown>>).filter(
    (l) => !String(l.nm).startsWith('flow:'),
  )

  const check = (prop: Prop | undefined, allowDelta: number, label: string) => {
    if (!prop || prop.a !== 1 || !Array.isArray(prop.k)) return
    const kfs = prop.k as Kf[]
    expect(kfs.length, label).toBeGreaterThan(1)
    const first = kfs[0]
    const last = kfs[kfs.length - 1]
    const same = JSON.stringify(first.s) === JSON.stringify(last.s)
    const delta =
      first.s?.length === 1 && last.s?.length === 1 ? Math.abs(last.s[0] - first.s[0]) : NaN
    const travels = Number.isFinite(delta) && delta > 0 && delta % allowDelta === 0
    expect(
      same || travels,
      `${label} 首尾未收口: ${JSON.stringify(first.s)} -> ${JSON.stringify(last.s)}`,
    ).toBe(true)
    expect(last.t, label).toBeLessThanOrEqual(anim.op as number)
  }

  for (const layer of layers) {
    const name = String(layer.nm)
    const ks = layer.ks as Record<string, Prop> | undefined
    if (ks) {
      check(ks.o, 360, `${name}.ks.o`)
      check(ks.s, 360, `${name}.ks.s`)
    }
    for (const group of (layer.shapes as Array<{ it: Array<Record<string, unknown>> }>) ?? []) {
      const items = group.it ?? []
      const stroke = items.find((i) => i.ty === 'st') as { d?: Array<{ v: Prop }> } | undefined
      const dashPeriod = stroke?.d ? Number(stroke.d[0].v.k) + Number(stroke.d[1].v.k) : 360
      for (const item of items) {
        if (item.ty === 'tr') {
          const tr = item as Record<string, Prop>
          check(tr.s, 360, `${name}.tr.s`)
          check(tr.r, 360, `${name}.tr.r`)
          check(tr.o, 360, `${name}.tr.o`)
        }
        if (item.ty === 'st' && stroke?.d) {
          check(stroke.d[2]?.v, dashPeriod, `${name}.dashOffset`)
        }
      }
    }
  }
}

describe('登录页 Lottie 资源(由 login-bg-left.svg 转换)', () => {
  const anim = load('login-dashboard.json')
  const layers = anim.layers as Array<Record<string, unknown>>
  const names = layers.map((l) => l.nm as string)

  it('合法 Lottie 且图层齐备、循环收口', () => {
    expect(anim.v).toBeTruthy()
    expect(anim.fr).toBeGreaterThan(0)
    expect(anim.w).toBe(366)
    expect(anim.h).toBe(384)
    expect(layers.length).toBeGreaterThan(80)
    for (const layer of layers) {
      expect(layer.ty).toBe(4)
      expect(layer.ks).toBeTruthy()
      expect(layer.ip as number).toBeGreaterThanOrEqual(anim.ip as number)
      expect(layer.op as number).toBeLessThanOrEqual(anim.op as number)
    }
    assertLoops(anim)
  })

  it('原图元素逐个转换到位(柱状/趋势线/饼图/卡片/星形)', () => {
    for (const id of [
      'Blue Bar 1',
      'Blue Bar 2',
      'Blue Bar 3',
      'Blue Bar 4',
      'Blue Bar 5',
      'Blue Bar 6',
      '直线 14',
      'Section 1',
      'Section 3',
      'Section 5',
      '椭圆 24',
      '矩形 745',
      '矩形 746',
      '矩形 747',
      '矩形 748',
      '星形 2',
      '减去顶层',
    ]) {
      expect(names).toContain(id)
    }
  })

  it('图层序:蓝色底卡在最底(数组末位),卡片辉光紧随卡片之后(位于其下方)', () => {
    expect(names[names.length - 1]).toBe('矩形 744')
    const cardAt = names.indexOf('矩形 746')
    expect(cardAt).toBeGreaterThan(-1)
    expect(names[cardAt + 1]).toMatch(/^shadow:矩形 746/)
  })

  it('趋势线流动高光:两端均不可见(窗口从 0 长度走出末端),故不闭合也无缝', () => {
    const flow = layers.find((l) => String(l.nm).startsWith('flow:')) as Record<string, unknown>
    expect(flow).toBeTruthy()
    const items = (flow.shapes as Array<{ it: Array<Record<string, unknown>> }>)[0].it
    const trim = items.find((i) => i.ty === 'tm') as { s: { k: Kf[] }; e: { k: Kf[] } }
    expect(trim).toBeTruthy()
    expect(trim.e.k[0].s?.[0]).toBe(0) // 首帧窗口长度 0 = 不可见(与原静态图一致)
    expect(trim.s.k[trim.s.k.length - 1].s?.[0]).toBeGreaterThanOrEqual(100) // 末帧已走出末端 = 不可见
  })
})
