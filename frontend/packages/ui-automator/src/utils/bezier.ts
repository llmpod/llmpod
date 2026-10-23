/**
 * 贝塞尔曲线鼠标路径生成工具
 * @description 生成模拟真实人类鼠标移动的贝塞尔曲线路径点集合
 */

import type { Point, BezierPathOptions } from '../types'

/** 默认路径生成选项 */
const DEFAULT_OPTIONS: Required<BezierPathOptions> = {
  points: 60,
  curvature: 0.3,
  noise: 0.08
}

/**
 * 生成三阶贝塞尔曲线控制点
 * 在起点和终点之间随机生成两个控制点，使路径自然弯曲
 */
function generateControlPoints(from: Point, to: Point, curvature: number): [Point, Point] {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const dist = Math.sqrt(dx * dx + dy * dy)

  // 法线方向（垂直于直线）
  const nx = -dy / dist
  const ny = dx / dist

  // 随机弯曲方向和强度
  const bend = (Math.random() - 0.5) * 2 * curvature * dist

  // 两个控制点分别在路径 1/3 和 2/3 处附近
  const cp1: Point = {
    x: from.x + dx * 0.3 + nx * bend,
    y: from.y + dy * 0.3 + ny * bend
  }
  const cp2: Point = {
    x: from.x + dx * 0.7 + nx * bend * 0.8,
    y: from.y + dy * 0.7 + ny * bend * 0.8
  }

  return [cp1, cp2]
}

/**
 * 三阶贝塞尔曲线插值（De Casteljau 算法）
 * @param t 参数 0-1
 * @param p0 起点
 * @param p1 控制点1
 * @param p2 控制点2
 * @param p3 终点
 */
function cubicBezier(t: number, p0: Point, p1: Point, p2: Point, p3: Point): Point {
  const mt = 1 - t
  const mt2 = mt * mt
  const mt3 = mt2 * mt
  const t2 = t * t
  const t3 = t2 * t

  return {
    x: mt3 * p0.x + 3 * mt2 * t * p1.x + 3 * mt * t2 * p2.x + t3 * p3.x,
    y: mt3 * p0.y + 3 * mt2 * t * p1.y + 3 * mt * t2 * p2.y + t3 * p3.y
  }
}

/**
 * 对点添加微小随机噪声（模拟手抖）
 */
function addPointNoise(point: Point, noiseStrength: number, dist: number): Point {
  if (noiseStrength === 0) return point
  const maxNoise = dist * noiseStrength * 0.05
  return {
    x: point.x + (Math.random() - 0.5) * maxNoise,
    y: point.y + (Math.random() - 0.5) * maxNoise
  }
}

/**
 * 生成从起点到终点的贝塞尔曲线鼠标路径点数组
 * @param from 起点坐标
 * @param to 终点坐标
 * @param options 路径生成选项
 * @returns 路径点数组（包含起点和终点）
 */
export function generateMousePath(from: Point, to: Point, options?: BezierPathOptions): Point[] {
  const opts = { ...DEFAULT_OPTIONS, ...options }
  const { points, curvature, noise } = opts

  // 距离极短时直接返回两点
  const dx = to.x - from.x
  const dy = to.y - from.y
  const dist = Math.sqrt(dx * dx + dy * dy)
  if (dist < 2) return [from, to]

  // 生成控制点
  const [cp1, cp2] = generateControlPoints(from, to, curvature)

  // 路径点数量根据距离动态调整（距离远则点更多）
  const adjustedPoints = Math.max(20, Math.min(points, Math.floor(dist / 5)))

  const path: Point[] = []
  for (let i = 0; i <= adjustedPoints; i++) {
    const t = i / adjustedPoints
    // 使用 ease-in-out 时间曲线（让移动在中间快两端慢）
    const easedT = easeInOutCubic(t)
    const point = cubicBezier(easedT, from, cp1, cp2, to)
    // 起点与终点保持精确坐标（锚定真实位置），中间点叠加手抖噪声
    const noisedPoint = i === 0 || i === adjustedPoints ? point : addPointNoise(point, noise, dist)
    path.push(noisedPoint)
  }

  // 确保最后一点精确等于终点
  path[path.length - 1] = { ...to }

  return path
}

/**
 * ease-in-out 缓动函数
 * 使鼠标在路径两端慢、中间快，更接近真实操作
 */
function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2
}

/**
 * 计算到达下一个路径点所需的时间（ms）
 * 根据两点之间的距离和速度系数动态计算，实现加速/减速效果
 * @param from 当前点
 * @param to 目标点
 * @param speedFactor 速度系数（越大越快）
 */
export function calculateStepDuration(from: Point, to: Point, speedFactor = 1.0): number {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const dist = Math.sqrt(dx * dx + dy * dy)
  // 基础：每像素 0.4ms，受速度系数影响
  const baseDuration = dist * 0.4
  return Math.max(5, baseDuration / Math.max(0.1, speedFactor))
}
