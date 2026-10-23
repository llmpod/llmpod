import { NColorPicker, NInput, NInputGroup } from 'naive-ui'
import {
  useForm,
  FormInput,
  FormCustom,
  FormButton,
  message,
  useTheme,
  useBrand,
  HEX_COLOR_RE,
  DEFAULT_THEME_CONFIG,
  DEFAULT_BRAND_CONFIG,
} from '@repo/hooks'
import { useStore } from './useStore'

/** 默认网页标题(平台名称清空时回落,与三端 index.html <title> 及 theme.boot 保持同值) */
const DEFAULT_DOCUMENT_TITLE = 'LLMPod'

/** 图片类字段输入上限(字符):浏览器对超长文本的插入与逐键编辑是线性昂贵操作,
 * 1MB 级 data:image base64 粘贴即可把整页输入管线拖到秒级卡死;超限在粘贴入口直接拒绝 */
const MAX_IMAGE_URL_LENGTH = 1_000_000

/** 超长粘贴拦截器:preventDefault 在浏览器插入文本前生效(零渲染成本),阻断而非截断 */
function rejectOversizedPaste(fieldLabel: string) {
  return (e: ClipboardEvent) => {
    const text = e.clipboardData?.getData('text') ?? ''
    if (text.length <= MAX_IMAGE_URL_LENGTH) return
    e.preventDefault()
    message.warning(
      `${fieldLabel}内容过长(上限 ${MAX_IMAGE_URL_LENGTH / 1_000_000}MB 字符):` +
        '内嵌 base64 图片请压缩后再粘贴,或改用 http(s) 图片地址',
    )
  }
}

/** 主题色选择器渲染函数:自定义色块触发器 + hex 输入框(供品牌/登录页两个表单复用) */
function renderColorField(field: 'themeColor' | 'loginPrimary') {
  return (ctx: {
    model: Record<string, string>
    setValues: (values: Record<string, string>) => void
  }) => (
    <div class="flex items-center gap-[.8rem]">
      <NColorPicker
        // 输入框逐字输入的非法中间态(#f、#f1f1 等)传入会让 seemly.rgba 抛错并打断渲染管线,
        // 只透传合法 3/6 位 hex,非法时以空串代替(ColorPicker 内部走空值分支)
        value={HEX_COLOR_RE.test(ctx.model[field]) ? ctx.model[field] : ''}
        onUpdate:value={(value: string) => ctx.setValues({ [field]: value })}
        modes={['hex']}
        showAlpha={false}
        class="w-auto"
      >
        {{
          trigger: ({ onClick }: { onClick: () => void }) => (
            <div
              class="h-[3.2rem] w-[4rem] cursor-pointer rounded-[4px] border border-black/10"
              style={{ backgroundColor: ctx.model[field] }}
              onClick={onClick}
            />
          ),
        }}
      </NColorPicker>
      <NInput
        value={ctx.model[field]}
        onUpdate:value={(value: string) => ctx.setValues({ [field]: value })}
        class="w-[120px]"
      />
    </div>
  )
}

export function useController() {
  const { settingsStore, toBrandModel, toLoginPageModel, saveBrandInfo, saveLoginPageConfig } =
    useStore()
  const { themeConfig, setThemeConfig, initRemoteTheme } = useTheme()
  const { setBrandConfig } = useBrand()

  const { LmForm: BrandForm, setValues: setBrandValues, formRef: brandFormRef } = useForm({
    // 主题色以全局主题状态回填(服务端未配置时显示当前生效色),品牌三字段以服务端回显为准
    data: () => ({
      ...toBrandModel(settingsStore.settings),
      themeColor: settingsStore.settings?.themeColor ?? themeConfig.value.primary,
    }),
    options: () => [
      FormInput('平台名称', 'platformName', {
        attrs: {
          class: 'max-w-[440px]',
          placeholder: '未配置时关于页显示默认「LLMPod」',
        },
      }),
      FormCustom(
        (ctx) => (
          <NInputGroup class="max-w-[440px]">
            <NInput
              value={ctx.model.logo}
              onUpdate:value={(value: string) => ctx.setValues({ logo: value })}
              maxlength={MAX_IMAGE_URL_LENGTH}
              // onPaste 走 inputProps 直绑内部 input 元素(NInput 根元素类型未声明 paste 事件)
              inputProps={{ onPaste: rejectOversizedPaste('Logo') }}
              placeholder="Emoji/URL,留空使用默认图标"
            />
          </NInputGroup>
        ),
        { wrapFormItem: true, label: 'Logo', field: 'logo' },
      ),
      FormCustom(renderColorField('themeColor'), {
        wrapFormItem: true,
        label: '主题色',
        field: 'themeColor',
        rules: [
          { required: true, message: '请选择主题色', trigger: 'change' },
          {
            pattern: HEX_COLOR_RE,
            message: '请输入 3/6 位 hex 颜色,如 #1261a8',
            trigger: 'change',
          },
        ],
      }),
      FormInput('页脚文案', 'footerText', {
        attrs: { class: 'max-w-[440px]', placeholder: '未配置时显示默认页脚' },
      }),
      FormButton('保存配置', {
        attrs: { type: 'primary', secondary: false },
        // 校验失败 reject 属于预期路径,吞掉避免未处理拒绝(同 tenant config)
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
      FormButton('重置默认', {
        attrs: { class: 'ml-[1rem]' },
        // 仅恢复表单默认值不落库,生效仍走「保存配置」;页脚文案回填默认值,其余可选字段空串保存即清空回落默认
        onClick: (ctx) => {
          ctx.setValues({
            platformName: '',
            logo: '',
            themeColor: DEFAULT_THEME_CONFIG.primary,
            footerText: DEFAULT_BRAND_CONFIG.footerText,
          })
          brandFormRef.value?.restoreValidation()
          message.info('已重置为默认值,保存后生效')
        },
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      const data = await saveBrandInfo(model)
      // 保存后立即生效:响应 data 喂全局状态;清空字段显式回落默认,
      // 与刷新后 theme.boot 缺键 + useStorage mergeDefaults 语义一致(空串会压掉默认,致侧栏标题/页脚消失)
      if (data.themeColor) setThemeConfig({ primary: data.themeColor })
      setBrandConfig({
        platformName: data.platformName || DEFAULT_BRAND_CONFIG.platformName,
        // logo 空值透传安全:消费方(LmLogo/favicon)自带空串回落,立即回落默认图标不必等下次刷新
        logo: data.logo || '',
        footerText: data.footerText || DEFAULT_BRAND_CONFIG.footerText,
      })
      // 网页标题同步三端语义:siteName(通用设置)优先,未配置回落平台名,再回落默认 LLMPod
      document.title = data.siteName || data.platformName || DEFAULT_DOCUMENT_TITLE
      message.success('配置已保存')
    },
  })

  const { LmForm: LoginPageForm, setValues: setLoginPageValues, formRef: loginPageFormRef } =
    useForm({
    data: () => toLoginPageModel(settingsStore.settings),
    options: () => [
      // 「登录页背景色」入口已下线(暂不开放设置):loginPrimary 不再渲染为表单项,
      // 但仍随表单原样透传,服务端已有配置不会被覆盖,登录页继续按该色渲染
      FormInput('背景图片 URL', 'loginBackground', {
        attrs: {
          class: 'max-w-[440px]',
          placeholder: '可选,支持 http(s)/data:image/相对路径,留空用默认背景',
          maxlength: MAX_IMAGE_URL_LENGTH,
          // onPaste 走 inputProps 直绑内部 input 元素,拦截发生在浏览器插入文本前
          inputProps: { onPaste: rejectOversizedPaste('背景图片') },
        },
        rules: [
          {
            trigger: 'change',
            // 兜底校验:粘贴拦截漏网路径(拖放/autofill/历史脏数据回显)超长时拦住保存
            validator: (_rule, value: string) =>
              !value || value.length <= MAX_IMAGE_URL_LENGTH
                ? Promise.resolve()
                : Promise.reject(new Error('背景图片地址过长,请改用 http(s) 图片 URL')),
          },
        ],
      }),
      FormButton('保存配置', {
        attrs: { type: 'primary', secondary: false },
        // 校验失败 reject 属于预期路径,吞掉避免未处理拒绝(同 tenant config)
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
      FormButton('重置默认', {
        attrs: { class: 'ml-[1rem]' },
        // 仅恢复表单默认值不落库,生效仍走「保存」:清空背景图回落默认;
        // 背景色不在表单内,重置不触碰(保存时仍原样透传服务端已配置值)
        onClick: (ctx) => {
          ctx.setValues({ loginBackground: '' })
          loginPageFormRef.value?.restoreValidation()
          message.info('已重置为默认值,保存后生效')
        },
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      const data = await saveLoginPageConfig(model)
      // 登录页配置立即生效:喂 remoteTheme 缓存(下次刷新 theme.boot 桥接同源)
      initRemoteTheme({
        loginBackground: data.loginBackground ?? '',
        loginPrimary: data.loginPrimary ?? '',
      })
      message.success('配置已保存')
    },
  })

  // 首次进入拉取回显,load 完成后把服务端值回填进两个表单
  void settingsStore.load()
  watch(
    () => settingsStore.settings,
    (settings) => {
      if (!settings) return
      setBrandValues({
        ...toBrandModel(settings),
        themeColor: settings.themeColor ?? themeConfig.value.primary,
      })
      setLoginPageValues(toLoginPageModel(settings))
    },
  )

  return { BrandForm, LoginPageForm }
}
