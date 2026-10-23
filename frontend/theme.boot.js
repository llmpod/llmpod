/**
 * 首屏主题启动脚本(内联进 index.html <head>,先于业务 bundle 同步执行)。
 * 由 vite.plugins.ts 的 injectThemeBootPlugin 注入,三端共用此单一来源。
 *
 * 职责:在 Vue 挂载前,用 localStorage 缓存的主题(后端下发的 app_remote_theme 主色,
 * 本地偏好 app_theme 的明暗 + app_theme_config 兜底)解析明暗并写入关键 CSS 变量,
 * 避免暗色用户白屏闪烁、后端主色未到达时的品牌色跳变。
 * 另:三端网页标题统一 siteName 优先(未配置回落 platformName,再回落 app_site 缓存,
 * 最后默认「LLMPod」),SEO meta description 取 siteDescription(同构回落);
 * dev 无 __LLMPOD_SETTINGS__ 注入/后端读失败时,app_site 缓存(admin config 页保存时写入)
 * 保证标题与描述不回退(各 index.html 静态 <title> 同值,仅作脚本未执行时的兜底)。
 *
 * 存储键与 packages/hooks/src/theme/useTheme.ts 保持一致,改动需两边同步。
 */
;(function () {
  var root = document.documentElement
  function read(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || 'null')
    } catch (e) {
      return null
    }
  }

  // 单键终态写:有字段 setItem 完整 seed,无字段 removeItem。
  // 决不"先删后写":removeItem 派发的 {newValue:null} 瞬态事件会让其它已挂载
  // useStorage(vueuse) 的标签页走 writeDefaults 分支把默认值写回 localStorage
  // (如 app_remote_theme 的 {}),覆盖本脚本紧随的 setItem —— 多标签下远程主题
  // (登录页背景图)因此丢失。seed 为完整对象整体替换,同样能清掉旧缓存中的过期键。
  function writeSeed(key, seed) {
    if (Object.keys(seed).length) localStorage.setItem(key, JSON.stringify(seed))
    else localStorage.removeItem(key)
  }

  // ---- window.__LLMPOD_SETTINGS__ 桥接(服务端注入于本脚本之前) ----
  // undefined = 后端读失败未注入,不动 localStorage 缓存;
  // null = 管理员未配置/已清空,清缓存回落默认;
  // 对象 = 有配置(仅开维护开关也算),每键按有值字段整体重写(单次终态写,见 writeSeed)。
  // 空字段不写键:useStorage 的 mergeDefaults 语义是"缺键回落默认、null 压掉默认"。
  var settings = window.__LLMPOD_SETTINGS__
  if (settings === null) {
    localStorage.removeItem('app_remote_theme')
    localStorage.removeItem('app_brand')
    localStorage.removeItem('app_site')
  } else if (settings) {
    var remoteSeed = {}
    if (settings.themeColor) remoteSeed.primary = settings.themeColor
    if (settings.loginBackground) remoteSeed.loginBackground = settings.loginBackground
    if (settings.loginPrimary) remoteSeed.loginPrimary = settings.loginPrimary
    var brandSeed = {}
    if (settings.platformName) brandSeed.platformName = settings.platformName
    if (settings.logo) brandSeed.logo = settings.logo
    if (settings.footerText) brandSeed.footerText = settings.footerText
    if (settings.siteDescription) brandSeed.siteDescription = settings.siteDescription
    var siteSeed = {}
    if (settings.siteName) siteSeed.siteName = settings.siteName
    if (settings.siteDescription) siteSeed.siteDescription = settings.siteDescription
    writeSeed('app_remote_theme', remoteSeed)
    writeSeed('app_brand', brandSeed)
    writeSeed('app_site', siteSeed)
  }
  // 站点缓存:dev 无注入/后端读失败(undefined)时标题与 SEO 描述的兜底源
  var siteCache = read('app_site') || {}
  // 网页标题:siteName(通用设置)优先,未配置回落 platformName,再回落 app_site 缓存,最后默认「LLMPod」
  document.title =
    (settings && (settings.siteName || settings.platformName)) || siteCache.siteName || 'LLMPod'
  // SEO 描述:siteDescription 未配置回落 app_site 缓存,再回落默认「LLMPod」,无标签时动态创建
  // (仅首屏此处维护:admin config 页保存后不再运行时改写,刷新后由本段恢复)
  var descriptionMeta = document.querySelector('meta[name="description"]')
  if (!descriptionMeta) {
    descriptionMeta = document.createElement('meta')
    descriptionMeta.setAttribute('name', 'description')
    document.head.appendChild(descriptionMeta)
  }
  descriptionMeta.setAttribute(
    'content',
    (settings && settings.siteDescription) || siteCache.siteDescription || 'LLMPod',
  )

  // ---- 品牌接管:favicon + 首屏 splash logo(与 packages/hooks/src/brand/useBrand.ts 等价,改动需两边同步) ----
  // 桥接后 app_brand 即最新品牌源(有配置=brandSeed,已清空=缺键,读失败=旧缓存),直接取缓存 logo。
  // 本脚本注入于 <head> 最前,执行时 <link rel="icon"> 与 splash 尚未解析:
  // favicon 待 DOM 就绪后在原 link 上改写;splash 用 MutationObserver 在插入瞬间替换(早于首帧绘制)。
  var brandLogo = ((read('app_brand') || {}).logo || '').trim()

  function applyFavicon() {
    var link = document.querySelector('link[rel="icon"]')
    if (!link) return
    if (!brandLogo) {
      link.type = 'image/x-icon'
      link.href = '/logo.ico'
      return
    }
    // 图片形态(http(s)/协议相对/data:image/根相对)直挂;其余 emoji/文本转 SVG data URI
    if (/^(https?:)?\/\/|^(data:image|\/)/i.test(brandLogo)) {
      var mime = /^data:(image\/[a-z+.-]+)(?:[;,]|$)/i.exec(brandLogo)
      if (mime) link.type = mime[1]
      else link.removeAttribute('type')
      link.href = brandLogo
      return
    }
    var safe = brandLogo
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
    link.type = 'image/svg+xml'
    link.href =
      'data:image/svg+xml,' +
      encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
          '<text y=".9em" font-size="90">' +
          safe +
          '</text></svg>',
      )
  }

  function applySplashBrand() {
    var box = document.querySelector('.app-splash__logo')
    if (!box) return false
    while (box.firstChild) box.removeChild(box.firstChild)
    if (/^(https?:)?\/\/|^(data:image|\/)/i.test(brandLogo)) {
      var img = document.createElement('img')
      img.src = brandLogo
      img.alt = 'logo'
      img.style.cssText = 'display:block;width:100%;height:100%;object-fit:contain'
      box.appendChild(img)
    } else {
      // 9.6rem 容器,字号取半与 about 页 emoji 比例一致
      box.style.cssText =
        'display:flex;align-items:center;justify-content:center;font-size:4.8rem;line-height:1'
      box.appendChild(document.createTextNode(brandLogo))
    }
    return true
  }

  if (brandLogo) {
    if (!applySplashBrand()) {
      var splashObserver = new MutationObserver(function () {
        if (applySplashBrand()) splashObserver.disconnect()
      })
      splashObserver.observe(document.documentElement, { childList: true, subtree: true })
      // 初始 HTML 解析完毕后 splash 不可能再出现,停止观察(门户端等无 splash 页面避免空转)
      document.addEventListener('DOMContentLoaded', function () {
        splashObserver.disconnect()
      })
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyFavicon)
  } else {
    applyFavicon()
  }

  // 明暗模式为纯前端本地偏好(app_theme),不消费后端下发的 mode。
  // useColorMode 以裸字符串持久化(非 JSON),与其它键的 JSON 读取不同,兼容历史带引号格式
  var remote = read('app_remote_theme') || {}
  var localConfig = read('app_theme_config') || {}
  var mode = (localStorage.getItem('app_theme') || '').replace(/^"|"$/g, '') || 'auto'
  if (mode !== 'light' && mode !== 'dark' && mode !== 'auto') mode = 'auto'
  var dark =
    mode === 'dark' ||
    (mode === 'auto' &&
      window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
  if (dark) {
    root.classList.add('dark')
    root.style.colorScheme = 'dark'
  }
  // 与 naive-ui commonLight/commonDark 的 bodyColor、baseColor/textColor2 一致,
  // 挂载后 addCssVarsToHtml 会用同值覆盖,不产生跳变
  root.style.setProperty('--n-body-color', dark ? '16,16,20' : '255,255,255')
  root.style.setProperty('--n-text-color', dark ? '255,255,255' : '0,0,0')

  // 主色:后端缓存优先,其次本地配置,最后默认品牌色(同 useTheme DEFAULT_THEME_CONFIG)
  var primary = remote.primary || localConfig.primary || '#1261a8'
  var hex = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(primary) ? primary.slice(1) : ''
  if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2]
  if (hex.length === 6) {
    var num = parseInt(hex, 16)
    root.style.setProperty('--c-primary', primary)
    root.style.setProperty(
      '--n-primary-color',
      ((num >> 16) & 255) + ',' + ((num >> 8) & 255) + ',' + (num & 255),
    )
  }
})()
