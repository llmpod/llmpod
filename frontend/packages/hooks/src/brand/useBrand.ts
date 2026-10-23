import { createSharedComposable, useStorage } from '@vueuse/core'
import { watch } from 'vue'

/** 品牌配置:暂无后端接口,先本地持久化;接入后端后可仿照 theme 的 remoteTheme 模式扩展。 */
export interface BrandConfig {
  /** 平台名称(关于页/品牌展示用) */
  platformName: string
  /** logo:Emoji 或图片 URL,空串表示未配置(展示产品默认 logo) */
  logo: string
  /** 页脚文案,空字符串表示不显示页脚(github 图标由 LmFooterText 恒显,不属于文案) */
  footerText: string
  /** 关于页副标文案,空串表示未配置(展示 DEFAULT_SITE_DESCRIPTION) */
  siteDescription: string
}

/** 关于页副标默认文案(站点描述未配置/清空时回落) */
export const DEFAULT_SITE_DESCRIPTION = '企业级大语言模型推理集群与算力编排中枢'

/** 默认品牌配置(页脚年份取页面加载时的当前年份)。 */
export const DEFAULT_BRAND_CONFIG: BrandConfig = {
  platformName: 'LLMPod',
  logo: '',
  footerText: `© ${new Date().getFullYear()} LLMPod. 保留所有权利。`,
  siteDescription: '',
}

const BRAND_STORAGE_KEY = 'app_brand'

/** 图片形态 logo:http(s)/协议相对/data:image/根相对路径;其余按 emoji 文本渲染 */
export const IMAGE_LOGO_RE = /^(https?:)?\/\/|^(data:image|\/)/i

/** favicon link(三端 index.html 均为 <link rel="icon">) */
const FAVICON_SELECTOR = 'link[rel="icon"]'

/** emoji/文本 → SVG data URI favicon(text 基线 .9em 近似垂直居中) */
function emojiFaviconUrl(logo: string): string {
  const safe = logo.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
    `<text y=".9em" font-size="90">${safe}</text></svg>`
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}

/**
 * 应用品牌 logo 到网页 favicon:图片 URL/data:image 直挂,emoji 转 SVG data URI,
 * 空值回落 /logo.ico。首屏由根目录 theme.boot.js 以等价逻辑先行(内联脚本无法复用本模块),
 * 改动需两边同步。
 */
export function applyBrandFavicon(logo: string): void {
  if (typeof document === 'undefined') return
  const link = document.querySelector<HTMLLinkElement>(FAVICON_SELECTOR)
  if (!link) return
  const value = logo.trim()
  if (!value) {
    link.type = 'image/x-icon'
    link.href = '/logo.ico'
    return
  }
  if (IMAGE_LOGO_RE.test(value)) {
    // data:image 前缀自带 MIME,其余移除 type 交浏览器按内容嗅探
    const mime = /^data:(image\/[a-z+.-]+)(?:[;,]|$)/i.exec(value)?.[1]
    if (mime) link.type = mime
    else link.removeAttribute('type')
    link.href = value
    return
  }
  link.type = 'image/svg+xml'
  link.href = emojiFaviconUrl(value)
}

/** 全局共享的品牌配置状态。 */
export const useBrand = createSharedComposable(() => {
  // 持久化到 localStorage,刷新或跨页面导航后仍保留
  const brandConfig = useStorage<BrandConfig>(
    BRAND_STORAGE_KEY,
    { ...DEFAULT_BRAND_CONFIG },
    undefined,
    { mergeDefaults: true },
  )

  // favicon 跟随品牌 logo:首屏由 theme.boot.js 先行应用,这里接管运行时变化(保存后立即生效)
  watch(
    () => brandConfig.value.logo,
    (logo) => applyBrandFavicon(logo ?? ''),
    { immediate: true },
  )

  /** 合并更新品牌配置。 */
  function setBrandConfig(config: Partial<BrandConfig>): void {
    brandConfig.value = { ...brandConfig.value, ...config }
  }

  return { brandConfig, setBrandConfig }
})
