import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/** 关于页 logo 的 hover Lottie 素材(与 tenant-web 同名同内容),契约与非空校验见下 */
const here = dirname(fileURLToPath(import.meta.url))
const asset = (app: string) => resolve(here, `../../${app}/public/animations/logo.json`)

interface Kf {
  t: number
  s?: number[]
  o?: { x: number[] }
  i?: { x: number[] }
}
interface Prop {
  a?: number
  k?: unknown
}
interface Layer {
  nm: string
  ty: number
  bm: number
  sr: number
  ind: number
  ip: number
  op: number
  ks: Record<string, Prop>
  shapes: Array<{ ty: string; it: Array<Record<string, unknown>> }>
}

function load(app: string): Record<string, unknown> {
  return JSON.parse(readFileSync(asset(app), 'utf8'))
}

/** 收集层内所有动画属性(值与本文档生成器一致:a === 1 且 k 为关键帧数组) */
function collectAnimated(node: unknown, out: Kf[][] = []): Kf[][] {
  if (Array.isArray(node)) {
    node.forEach((child) => collectAnimated(child, out))
    return out
  }
  if (!node || typeof node !== 'object') return out
  const record = node as Record<string, unknown>
  if (record.a === 1 && Array.isArray(record.k)) {
    out.push(record.k as Kf[])
    return out
  }
  for (const value of Object.values(record)) collectAnimated(value, out)
  return out
}

describe('关于页 logo Lottie 素材', () => {
  const anim = load('admin-web')
  const layers = anim.layers as Layer[]
  const names = layers.map((layer) => layer.nm)

  it('两个 app 的素材完全一致(同一份 hover 动效)', () => {
    expect(readFileSync(asset('tenant-web'))).toEqual(readFileSync(asset('admin-web')))
  })

  it('合法 Lottie:400×400 画布、36 帧、非等分关键帧末帧落在 op-1 且终态为静态', () => {
    expect(anim.v).toBeTruthy()
    expect(anim.fr).toBe(30)
    expect(anim.w).toBe(400)
    expect(anim.h).toBe(400)
    expect(anim.ip).toBe(0)
    expect(anim.op).toBe(36)

    const props = layers.flatMap((layer) => collectAnimated(layer))
    expect(props.length).toBeGreaterThan(0)
    for (const kfs of props) {
      expect(kfs.length).toBeGreaterThan(0)
      kfs.forEach((frame, index) => {
        expect(Number.isFinite(frame.t)).toBe(true)
        expect(frame.s?.length).toBeGreaterThan(0)
        if (index > 0) expect(frame.t).toBeGreaterThan(kfs[index - 1].t)
        // 末帧落在 35(op-1):播完的终态与静态兜底图完全一致,不留残影
        if (index === kfs.length - 1) expect(frame.t).toBeLessThanOrEqual(35)
        else expect(Array.isArray(frame.o?.x) && Array.isArray(frame.i?.x)).toBe(true)
      })
    }
  })

  it('终态还原静态:透明度/缩放末帧为满值,bolt 位移末帧回到轴心原位', () => {
    const bolt = layers.find((layer) => layer.nm === 'bolt') as Layer
    const barL = layers.find((layer) => layer.nm === 'bar-l') as Layer
    const hexRing = layers.find((layer) => layer.nm === 'hex-ring') as Layer

    for (const layer of [bolt, barL, hexRing]) {
      const opacity = layer.ks.o
      if (opacity.a === 1) expect(opacity.k!.at(-1)!.s).toEqual([100])
      const scale = layer.ks.s
      if (scale.a === 1) expect(scale.k!.at(-1)!.s).toEqual([100, 100, 100])
    }
    // bolt 位移滑入终态 = 静态锚心
    const anchor = (bolt.ks.a.k as number[]).slice(0, 2)
    const lastP = (bolt.ks.p.k as Kf[]).at(-1)!.s!
    expect(lastP.slice(0, 2)).toEqual(anchor)
  })

  it('图层结构就位:闪电钩 + 左柱 + 六边形环框,ind 唯一且等于下标 +1', () => {
    expect(names).toEqual(['bolt', 'bar-l', 'hex-ring'])
    layers.forEach((layer, index) => {
      expect(layer.ty).toBe(4)
      expect(layer.sr).toBe(1)
      expect(layer.bm).toBe(0)
      expect(layer.ind).toBe(index + 1)
      expect(layer.ip).toBeGreaterThanOrEqual(anim.ip as number)
      expect(layer.op).toBeLessThanOrEqual(anim.op as number)
      expect(layer.shapes.length).toBeGreaterThan(0)
    })
  })

  it('六边形环框 = 外轮廓 + 内孔两个形状,evenodd 填充挖孔', () => {
    const hexRing = layers.find((layer) => layer.nm === 'hex-ring') as Layer
    const shs = hexRing.shapes[0].it.filter((item) => item.ty === 'sh')
    expect(shs.length).toBe(2)
    const fills = hexRing.shapes[0].it.filter((item) => item.ty === 'fl')
    expect(fills.length).toBe(1)
    expect(fills[0].r).toBe(2) // evenodd
  })

  it('左柱以柱底为缩放轴心(scaleY 从底升起),轴心 x 居柱中、y 为柱底', () => {
    const layer = layers.find((item) => item.nm === 'bar-l') as Layer
    const [ax, ay] = (layer.ks.a.k as number[]).slice(0, 2)
    const [px, py] = (layer.ks.p.k as number[]).slice(0, 2)
    expect([px, py]).toEqual([ax, ay])
    const path = layer.shapes[0].it.find((item) => item.ty === 'sh') as {
      ks: { k: { v: number[][] } }
    }
    const xs = path.ks.k.v.map((point) => point[0])
    const ys = path.ks.k.v.map((point) => point[1])
    expect(ax).toBeCloseTo((Math.min(...xs) + Math.max(...xs)) / 2, 1)
    expect(ay).toBeCloseTo(Math.max(...ys), 1)
    expect(layer.ks.s.a).toBe(1) // scaleY 动画
  })

  it('单色品牌蓝填充:fc 分量为 rgb(4,115,246) 且每层恰好一个', () => {
    for (const layer of layers) {
      const fills = layer.shapes[0].it.filter((item) => item.ty === 'fl')
      expect(fills.length).toBe(1)
      const fill = fills[0].c as { a: number; k: number[] }
      expect(fill.a).toBe(0)
      expect(fill.k.length).toBe(4)
      expect(fill.k[0]).toBeCloseTo(4 / 255, 5)
      expect(fill.k[1]).toBeCloseTo(115 / 255, 5)
      expect(fill.k[2]).toBeCloseTo(246 / 255, 5)
      expect(fill.k[3]).toBe(1)
    }
  })
})
