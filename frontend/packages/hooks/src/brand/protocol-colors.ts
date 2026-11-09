/**
 * 上游接入协议的品牌固定色。
 *
 * 品牌色不跟随主题 token —— 明暗切换时也要保持供应商识别度,所以不走 `--c-*` / `--n-*`,
 * 而是集中成常量维护。同一协议色会在多个 tag(接入地址、支持接口、模型详情)上复用,
 * 分散手写会随迭代漂移,故统一从这里取。
 */
export const PROTOCOL_BRAND_HEX = {
  openai: '#10A37F',
  anthropic: '#D97757',
} as const

export type ProtocolBrand = keyof typeof PROTOCOL_BRAND_HEX

/** n-tag 的品牌色配置:文字与描边同色,底色透明(底色交给主题卡片) */
export interface ProtocolTagColor {
  textColor: string
  borderColor: string
  color: 'transparent'
}

/** 取指定协议在 tag 上使用的品牌色配置 */
export function protocolTagColor(protocol: ProtocolBrand): ProtocolTagColor {
  const hex = PROTOCOL_BRAND_HEX[protocol]
  return { textColor: hex, borderColor: hex, color: 'transparent' }
}
