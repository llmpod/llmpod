import { describe, expect, it } from 'vitest'
import { checkActiveQuery } from '../src/components/table/utils'

const base = {
  initialParam: { keyword: '', status: '' },
  filterKeyword: '',
  filters: null,
}

describe('checkActiveQuery', () => {
  it('param 与初始值一致且无筛选时为 false', () => {
    expect(checkActiveQuery({ ...base, param: { keyword: '', status: '' } })).toBe(false)
  })

  it('param 值偏离初始值时为 true', () => {
    expect(checkActiveQuery({ ...base, param: { keyword: 'abc', status: '' } })).toBe(true)
    expect(checkActiveQuery({ ...base, param: { keyword: '', status: 'active' } })).toBe(true)
  })

  it('空值归一:初始 "" 与当前 undefined/null 视为一致', () => {
    expect(checkActiveQuery({ ...base, param: { keyword: undefined, status: null } })).toBe(false)
  })

  it('param 多出初始没有的 key 且有值时为 true', () => {
    expect(checkActiveQuery({ ...base, param: { keyword: '', status: '', extra: 1 } })).toBe(true)
  })

  it('filterKeyword 非空为 true,仅空白字符为 false', () => {
    expect(checkActiveQuery({ ...base, filterKeyword: 'abc' })).toBe(true)
    expect(checkActiveQuery({ ...base, filterKeyword: '   ' })).toBe(false)
  })

  it('列筛选有有效值为 true,空数组/null 为 false', () => {
    expect(checkActiveQuery({ ...base, filters: { status: ['active'] } })).toBe(true)
    expect(checkActiveQuery({ ...base, filters: { status: [] } })).toBe(false)
    expect(checkActiveQuery({ ...base, filters: { status: null } })).toBe(false)
  })
})
