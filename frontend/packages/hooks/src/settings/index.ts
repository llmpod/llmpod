/**
 * 平台全局设置载荷：
 * - GET /api/v1/settings 与 POST /api/v1/admin/settings/update 的响应 data
 * - 服务端注入 index.html 的 window.__LLMPOD_SETTINGS__（theme.boot.js 桥接消费，
 *   注入侧额外携带只读附加键 serverVersion / first_guide，见 LlmpodSettingsSeed）
 *
 * 12 字段全量、键序固定（后端测试锁定）；除 maintenanceMode / telemetryEnabled 外
 * 均可空，null / 空串 = 未配置，前端回落各自默认值。字段约束（hex / mode 枚举 /
 * scheme 白名单 / 限长）由后端写路径净化，前端拿到即合法值。
 */
export interface LlmpodSettings {
  /** 站点名（浏览器标题优先来源；通用设置空白保存自动回落「LLMPod」） */
  siteName: string | null
  /** 站点描述(关于页副标,无 SEO 作用;通用设置清空保存落 null,前端回落默认副标) */
  siteDescription: string | null
  /** 品牌名称(关于页展示;浏览器标题次级回落,siteName 未配置时经 theme.boot 生效) */
  platformName: string | null
  /** logo：URL / data:image / 相对路径 / emoji 文本 */
  logo: string | null
  /** 页脚文案 */
  footerText: string | null
  /** 全站主色 #rgb / #rrggbb（登录页之外所有页面） */
  themeColor: string | null
  /** 明暗模式；null = 跟随用户本地偏好 */
  mode: 'light' | 'dark' | 'auto' | null
  /** 登录页背景：图片 URL / data:image（走 LoginBackground image 分支） */
  loginBackground: string | null
  /** 登录页主色 hex（走 LoginBackground color 分支）；null 回落 themeColor */
  loginPrimary: string | null
  /** 维护开关（必有键，false 不缺省） */
  maintenanceMode: boolean
  /** 维护文案；null 时展示默认「系统维护中」 */
  maintenanceMessage: string | null
  /**
   * 用户体验改进计划（匿名遥测）开关；缺省 true，键必有。
   * 旧缓存种子/旧版后端可能不带此键，消费方按 `?? true` 回落默认开启。
   */
  telemetryEnabled: boolean
}

declare global {
  interface Window {
    /**
     * 服务端注入到每份 index.html 的设置种子（theme.boot.js 之前执行）：
     * - 对象：有配置（仅开维护开关也算），值 ≡ GET /settings 的 data + 末位只读附加键 serverVersion
     * - null：管理员未配置/已清空，前端清缓存回落默认（此时不带版本）
     * - undefined：后端 DB 读失败未注入，沿用 localStorage 缓存
     */
    __LLMPOD_SETTINGS__?: LlmpodSettingsSeed | null
  }
}

/**
 * index.html 注入种子专用类型：LlmpodSettings + 末位只读附加键。
 * serverVersion（平台版本，calver 如 26.9.3）**只出现在注入变量**，
 * GET /api/v1/settings 的 data 不含此字段——勿把它加进设置接口的响应类型。
 */
export type LlmpodSettingsSeed = LlmpodSettings & {
  /** 平台运行版本（只读，只要有配置恒有值）；管理员未配置时整个种子为 null，取不到 */
  serverVersion?: string
  /**
   * 首次引导开关（snake_case，只出现在注入变量）：true = 平台从未走过
   * 快速设置向导，管理端登录落地 dashboard 时由守卫自动改道 /quick-setup。
   * 设置 API 响应不含此字段——勿把它加进设置接口的响应类型。
   */
  first_guide?: boolean
}
