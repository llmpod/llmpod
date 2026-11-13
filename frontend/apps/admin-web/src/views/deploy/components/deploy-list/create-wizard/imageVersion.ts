import type { ImageEntryOut } from '@/api/inference-image'

/** 版本下拉项在 n-select 里的行 class(hover 时显示「驱动要求」的锚点,样式见 StepBasic 全局块) */
export const IMAGE_VERSION_OPTION_CLASS = 'deploy-image-version-option'

/** 镜像版本下拉项:同名版本的多条 catalog 变体(按 arch/gpu_model/driver 区分)聚合后的一行 */
export interface ImageVersionOption {
  label: string
  value: string
  /** n-select 选项行 class(naive-ui 会把 rawNode.class 合到选项 div 上) */
  class: string
  /** 该版本涉及的架构原值(arch 原文,去重保序) */
  archs: string[]
  /** 驱动要求行(按架构分组,cann 再按 GPU 型号细分),悬浮展示用 */
  drivers: { group: string; driver: string }[]
}

/**
 * 驱动要求分组名:arch 原值即分组名;
 * cann 带具体 GPU 型号时细分为「cann 310P」这类(通配 * 表示不细分,归到 arch 本身)。
 * catalog 的 gpu_model 是 fnmatch glob(如 `*310P*`),展示前去掉通配符。
 */
export function driverGroup(entry: ImageEntryOut): string {
  const models = (entry.gpu_model ?? [])
    .map((model) => model.replace(/\*/g, '').trim())
    .filter(Boolean)
  return entry.arch === 'cann' && models.length ? `cann ${models.join('/')}` : entry.arch
}

/**
 * 驱动区间规范化展示:catalog 原值是 `>=580` / `>=530,<570` 这类 ASCII 比较符,
 * 展示层统一成 ≥ / ≤ 单字符(与产品文案一致),其余字符原样保留。
 */
export function formatDriver(driver: string): string {
  return driver.replace(/>=/g, '≥').replace(/<=/g, '≤')
}

/**
 * 镜像目录 → 版本下拉项(catalog 同 engine_version 的多 gpu_model/driver 变体合并成一行):
 * - archs 收该版本出现的全部架构原值,供标签展示;
 * - drivers 按「分组 + 驱动下限」去重,供「驱动要求」悬浮按架构分组展示;
 * - 无 engine_version 的条目(异常数据)跳过。
 */
export function buildImageVersionOptions(entries: ImageEntryOut[]): ImageVersionOption[] {
  const versions = new Map<string, ImageVersionOption>()
  for (const entry of entries) {
    if (!entry.engine_version) continue
    let option = versions.get(entry.engine_version)
    if (!option) {
      option = {
        label: entry.engine_version,
        value: entry.engine_version,
        class: IMAGE_VERSION_OPTION_CLASS,
        archs: [],
        drivers: [],
      }
      versions.set(entry.engine_version, option)
    }
    if (entry.arch && !option.archs.includes(entry.arch)) option.archs.push(entry.arch)
    const group = driverGroup(entry)
    if (entry.driver && !option.drivers.some((row) => row.group === group && row.driver === entry.driver)) {
      option.drivers.push({ group, driver: entry.driver })
    }
  }
  return [...versions.values()]
}