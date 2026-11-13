import { describe, expect, it } from 'vitest'
import type { ImageEntryOut } from '@/api/inference-image'
import {
  buildImageVersionOptions,
  driverGroup,
  IMAGE_VERSION_OPTION_CLASS,
} from '@/views/deploy/components/deploy-list/create-wizard/imageVersion'

function entry(overrides: Partial<ImageEntryOut> = {}): ImageEntryOut {
  return {
    id: 'img-1',
    engine: 'vllm',
    arch: 'nvidia',
    gpu_model: ['*'],
    driver: '>=580',
    engine_version: '0.23.0',
    image: 'reg/vllm:0.23.0-nvidia',
    default: false,
    notes: '',
    ...overrides,
  } as ImageEntryOut
}

describe('imageVersion 聚合(第 3 点:版本下拉显示架构标签与驱动要求)', () => {
  it('同版本多变体合并为一行,架构标签按 arch 原值去重保序', () => {
    const options = buildImageVersionOptions([
      entry({ id: 'a', arch: 'nvidia', gpu_model: ['*'], driver: '>=580' }),
      entry({ id: 'b', arch: 'cann', gpu_model: ['*'], driver: '>=580' }),
      entry({ id: 'c', arch: 'cann', gpu_model: ['*'], driver: '>=580' }),
      entry({ id: 'd', engine_version: '0.24.0', arch: 'nvidia', image: 'reg/vllm:0.24.0' }),
    ])

    expect(options.map((option) => option.value)).toEqual(['0.23.0', '0.24.0'])
    expect(options[0].archs).toEqual(['nvidia', 'cann'])
    // 同分组同驱动只留一行(两条 cann 通配变体不重复),但 nvidia/cann 是两个分组各一行
    expect(options[0].drivers).toEqual([
      { group: 'nvidia', driver: '>=580' },
      { group: 'cann', driver: '>=580' },
    ])
  })

  it('cann 带具体 GPU 型号时按型号细分驱动要求', () => {
    const options = buildImageVersionOptions([
      entry({ id: 'a', arch: 'nvidia', gpu_model: ['*'], driver: '≥580' }),
      entry({ id: 'b', arch: 'cann', gpu_model: ['310P'], driver: '≥25.2' }),
      entry({ id: 'c', arch: 'cann', gpu_model: ['910B', '910C'], driver: '≥24.1' }),
      entry({ id: 'd', arch: 'cann', gpu_model: ['950DT'], driver: '≥26.0' }),
    ])

    expect(options).toHaveLength(1)
    expect(options[0].drivers).toEqual([
      { group: 'nvidia', driver: '≥580' },
      { group: 'cann 310P', driver: '≥25.2' },
      { group: 'cann 910B/910C', driver: '≥24.1' },
      { group: 'cann 950DT', driver: '≥26.0' },
    ])
  })

  it('driverGroup:cann 通配机型不细分,其它架构一律用 arch 原值,glob 通配符去掉', () => {
    expect(driverGroup(entry({ arch: 'cann', gpu_model: ['*'] }))).toBe('cann')
    expect(driverGroup(entry({ arch: 'cann', gpu_model: ['*310P*'] }))).toBe('cann 310P')
    expect(driverGroup(entry({ arch: 'cann', gpu_model: ['*910B*', '*A2*'] }))).toBe('cann 910B/A2')
    expect(driverGroup(entry({ arch: 'nvidia', gpu_model: ['A100'] }))).toBe('nvidia')
  })

  it('缺 driver 时不产生驱动要求行;缺 engine_version 的条目跳过', () => {
    const options = buildImageVersionOptions([
      entry({ driver: null }),
      entry({ id: 'x', engine_version: null }),
    ])
    expect(options).toHaveLength(1)
    expect(options[0].drivers).toEqual([])
  })

  it('每个选项带行 class(hover 显示「驱动要求」的锚点)', () => {
    const options = buildImageVersionOptions([entry()])
    expect(options[0].class).toBe(IMAGE_VERSION_OPTION_CLASS)
  })
})