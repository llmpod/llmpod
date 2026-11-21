export { default as LmVirtualText } from './LmVirtualText.vue'
export type { LmVirtualTextProps } from './LmVirtualText.vue'
export {
  DEFAULT_MAX_LINE_CHARS,
  DEFAULT_MAX_VIRTUAL_LINES,
  DEFAULT_PRETTY_LIMIT,
  estimateVirtualTextHeight,
  formatTextSize,
  prepareVirtualText,
  splitTextToVirtualLines,
  stringifyVirtualContent,
} from './format'
export type {
  EstimateHeightOptions,
  PreparedVirtualText,
  PrepareVirtualTextOptions,
  VirtualTextLine,
} from './format'
