import { describe, expect, it } from 'vitest'
import { formatDecimal, formatSignedAmount } from '../src/tools/decimal'

describe('formatDecimal(Decimal 字符串去尾零,纯字符串实现)', () => {
  it('常规去尾零', () => {
    expect(formatDecimal('12.500000')).toBe('12.5')
    expect(formatDecimal('0.000000')).toBe('0')
    expect(formatDecimal('100.000000')).toBe('100')
    expect(formatDecimal('0.001500')).toBe('0.0015')
    expect(formatDecimal('2.000000')).toBe('2')
    expect(formatDecimal('234.500000')).toBe('234.5')
  })

  it('小数值不进科学计数法(UR-001 核心:Number() 中转会产出 5e-7)', () => {
    expect(formatDecimal('0.0000005')).toBe('0.0000005')
    expect(formatDecimal('0.000000000000000000000001')).toBe('0.000000000000000000000001')
    expect(formatDecimal('-0.0000005')).toBe('-0.0000005')
  })

  it('超大整数不进科学计数法(≥1e21 时 String(Number()) 产出 1e+21)', () => {
    expect(formatDecimal('1000000000000000000000')).toBe('1000000000000000000000')
    expect(formatDecimal('123456789012345678901234567890.500000')).toBe(
      '123456789012345678901234567890.5',
    )
  })

  it('符号与前导零归一', () => {
    expect(formatDecimal('+7.10')).toBe('7.1')
    expect(formatDecimal('-0.000000')).toBe('0')
    expect(formatDecimal('-0.012500')).toBe('-0.0125')
    expect(formatDecimal('007.500')).toBe('7.5')
  })

  it('空值显示 —;非十进制定点字符串原样返回', () => {
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal(undefined)).toBe('—')
    expect(formatDecimal('')).toBe('—')
    expect(formatDecimal('abc')).toBe('abc')
    // 科学计数法输入非常规,原样返回不在展示层二次加工
    expect(formatDecimal('5e-7')).toBe('5e-7')
  })
})

describe('formatSignedAmount(变动金额:正数加 +,去尾零)', () => {
  it('正数加 +,负数保留 -,零不加符号', () => {
    expect(formatSignedAmount('100.000000')).toBe('+100')
    expect(formatSignedAmount('-0.012500')).toBe('-0.0125')
    expect(formatSignedAmount('0.000000')).toBe('0')
    expect(formatSignedAmount('-0.000000')).toBe('0')
    expect(formatSignedAmount('234.487500')).toBe('+234.4875')
  })

  it('小数值不进科学计数法', () => {
    expect(formatSignedAmount('0.0000005')).toBe('+0.0000005')
    expect(formatSignedAmount('-0.0000005')).toBe('-0.0000005')
  })

  it('非数字原样返回', () => {
    expect(formatSignedAmount('abc')).toBe('abc')
  })
})
