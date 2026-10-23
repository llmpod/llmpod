import {
  useForm,
  FormInput,
  FormSwitch,
  FormCustom,
  FormButton,
  message,
  useBrand,
  useConfirm,
  type LlmpodSettings,
} from '@repo/hooks'
import { useStore } from './useStore'

/** 默认站点名(与三端 index.html <title> 及 theme.boot 保持同值) */
const DEFAULT_SITE_TEXT = 'LLMPod'

/**
 * 站点字段写 app_site 缓存,供 theme.boot 刷新兜底:
 * dev 无 __LLMPOD_SETTINGS__ 注入/后端读失败时,标题与 SEO 描述从该缓存恢复(与 boot 桥接同构)。
 */
function writeSiteCache(data: Pick<LlmpodSettings, 'siteName' | 'siteDescription'>) {
  const seed: Record<string, string> = {}
  if (data.siteName) seed.siteName = data.siteName
  if (data.siteDescription) seed.siteDescription = data.siteDescription
  localStorage.setItem('app_site', JSON.stringify(seed))
}

export function useController() {
  const { settingsStore, toGeneralModel, toMaintenanceModel, saveGeneral, saveMaintenance } =
    useStore()
  const { setBrandConfig } = useBrand()

  const { LmForm, setValues } = useForm({
    data: () => toGeneralModel(settingsStore.settings),
    options: () => [
      FormInput('站点名称', 'siteName', {
        attrs: {
          class: 'max-w-[440px]',
          placeholder: '留空默认「LLMPod」',
          maxlength: 100,
        },
      }),
      FormInput('站点描述', 'siteDescription', {
        attrs: {
          class: 'max-w-[440px]',
          placeholder: '关于页副标展示,留空用默认描述',
          maxlength: 500,
        },
      }),
      FormButton('保存设置', {
        attrs: { type: 'primary', secondary: false },
        // 校验失败 reject 属于预期路径,吞掉避免未处理拒绝(同 tenant config)
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      const data = await saveGeneral(model)
      // 站点名保存后立即生效:本端更新标题并写 app_site 缓存
      // (dev 无注入种子,刷新后由 theme.boot 读缓存恢复,不回退默认)
      document.title = data.siteName || DEFAULT_SITE_TEXT
      writeSiteCache(data)
      // 站点描述仅用于关于页副标(无 SEO 作用):空值透传空串,关于页回落默认文案
      setBrandConfig({ siteDescription: data.siteDescription || '' })
      message.success('设置已保存')
    },
  })

  const { LmForm: MaintenanceForm, setValues: setMaintenanceValues } = useForm({
    data: () => toMaintenanceModel(settingsStore.settings),
    options: () => [
      FormSwitch('维护模式', 'maintenanceMode'),
      FormCustom(
        () => (
          <span class="text-secondary text-base">
            开启后将拦截全部非平台域登录与租户调用,仅平台域超管可登录
          </span>
        ),
        { wrapFormItem: true, label: '维护模式说明' },
      ),
      FormInput('维护文案', 'maintenanceMessage', {
        attrs: {
          class: 'max-w-[440px]',
          placeholder: '可选,留空显示「系统维护中」',
          maxlength: 200,
          showCount: true,
        },
      }),
      FormButton('保存维护设置', {
        attrs: { type: 'primary', secondary: false },
        onClick: (ctx) => ctx.submit().catch(() => {}),
      }),
    ],
    submit: async (model, validate) => {
      await validate()
      // 开启维护是高危操作(误开会拦全平台登录),二次确认
      if (model.maintenanceMode && !settingsStore.settings?.maintenanceMode) {
        try {
          await useConfirm({
            title: '开启维护模式',
            content: '开启后将拦截全部非平台域登录与租户调用,确认开启?',
            confirmText: '开启维护',
            cancelText: '取消',
          })
        } catch {
          return
        }
      }
      await saveMaintenance(model)
      message.success(model.maintenanceMode ? '已开启维护模式' : '维护设置已保存')
    },
  })

  // 首次进入拉取回显,load 完成后把服务端值回填进两个表单
  void settingsStore.load()
  watch(
    () => settingsStore.settings,
    (settings) => {
      if (!settings) return
      setValues(toGeneralModel(settings))
      setMaintenanceValues(toMaintenanceModel(settings))
    },
  )

  return { LmForm, MaintenanceForm }
}
