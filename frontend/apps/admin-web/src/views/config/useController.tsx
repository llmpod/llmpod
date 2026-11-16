import {
  useForm,
  FormInput,
  FormInputNumber,
  FormRadioGroup,
  FormSwitch,
  FormCustom,
  FormButton,
  message,
  useBrand,
  useConfirm,
  SITE_STORAGE_KEY,
  type LlmpodSettings,
} from '@repo/hooks'
import {
  useStore,
  isValidBodyMaxGib,
  type BodyLogModel,
  type BodyRetentionMode,
} from './useStore'

/** 默认站点名(与三端 index.html <title> 及 theme.boot 保持同值) */
const DEFAULT_SITE_TEXT = 'LLMPod'

/** body 块轮转方式选项（后端枚举，非法值 422） */
const RETENTION_MODE_OPTIONS: { label: string; value: BodyRetentionMode }[] = [
  { label: '按时间', value: 'time' },
  { label: '按大小', value: 'size' },
]

/**
 * 站点字段写 app_site 缓存,供 theme.boot 刷新兜底:
 * dev 无 __LLMPOD_SETTINGS__ 注入/后端读失败时,标题与 SEO 描述从该缓存恢复(与 boot 桥接同构)。
 */
function writeSiteCache(data: Pick<LlmpodSettings, 'siteName' | 'siteDescription'>) {
  const seed: Record<string, string> = {}
  if (data.siteName) seed.siteName = data.siteName
  if (data.siteDescription) seed.siteDescription = data.siteDescription
  localStorage.setItem(SITE_STORAGE_KEY, JSON.stringify(seed))
}

export function useController() {
  const {
    settingsStore,
    toGeneralModel,
    toMaintenanceModel,
    toBodyLogModel,
    saveGeneral,
    saveMaintenance,
    saveBodyLog,
  } = useStore()
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

  /** 记录开关镜像：关闭时只保留开关，轮转设置整块不渲染 */
  const bodyLogEnabled = ref(toBodyLogModel(settingsStore.settings).bodySnapshot)

  /**
   * 轮转方式镜像:数值行随它整行替换(time → 保留天数 / size → 容量上限)。
   * 回显 mode 为 null 时按「按时间」展示,但不回写后端,除非用户真的切了 radio。
   */
  const bodyLogMode = ref<BodyRetentionMode>(
    toBodyLogModel(settingsStore.settings).bodyRetentionMode,
  )

  const {
    LmForm: BodyLogForm,
    setValues: setBodyLogValues,
    params: bodyLogParams,
  } = useForm<BodyLogModel>({
    data: () => toBodyLogModel(settingsStore.settings),
    options: () =>
      computed(() => {
        const isTimeMode = bodyLogMode.value === 'time'
        return [
          FormSwitch('记录请求体', 'bodySnapshot'),
          // 关闭记录时不展示轮转设置(规则仍作用于存量块,重新开启即可编辑)
          ...(bodyLogEnabled.value
            ? [
                FormRadioGroup('轮转方式', 'bodyRetentionMode', RETENTION_MODE_OPTIONS),
                // 数值行整体替换:仅 time 消费天数 / 仅 size 消费容量,清空即回落系统默认
                ...(isTimeMode
                  ? [
                      FormInputNumber('保留天数', 'bodyRetentionDays', {
                        attrs: {
                          class: 'w-[12rem]',
                          min: 1,
                          precision: 0,
                          clearable: true,
                          placeholder: '系统默认',
                        },
                        controlSlots: { suffix: () => '天' },
                        rules: [
                          {
                            validator: (_rule: unknown, value: number | null) =>
                              value == null || (Number.isInteger(value) && value >= 1)
                                ? true
                                : new Error('保留天数至少 1 天'),
                            trigger: ['blur', 'change'],
                          },
                        ],
                      }),
                    ]
                  : [
                      FormInputNumber('容量上限', 'bodyMaxBytes', {
                        attrs: {
                          class: 'w-[12rem]',
                          min: 0,
                          precision: 1,
                          clearable: true,
                          placeholder: '系统默认',
                        },
                        controlSlots: { suffix: () => 'GiB' },
                        rules: [
                          {
                            validator: (_rule: unknown, value: number | null) =>
                              isValidBodyMaxGib(value) ? true : new Error('容量上限至少 1 MiB'),
                            trigger: ['blur', 'change'],
                          },
                        ],
                      }),
                    ]),
              ]
            : []),
          FormButton('保存调用日志设置', {
            attrs: { type: 'primary', secondary: false },
            onClick: (ctx) => ctx.submit().catch(() => {}),
          }),
        ]
      }),
    submit: async (model, validate) => {
      await validate()
      const saving = saveBodyLog(model)
      if (!saving) {
        message.info('没有需要保存的改动')
        return
      }
      await saving
      // 网关按配置轮询热加载(默认 3 秒),无需重启
      message.success('调用日志设置已保存,网关将在数秒内热生效')
    },
  })

  // 开关/radio 变更跟随表单值(回填/复位同样驱动轮转设置显隐与数值行切换)
  watch(
    () => bodyLogParams().bodySnapshot,
    (enabled) => {
      bodyLogEnabled.value = enabled
    },
    { immediate: true },
  )
  watch(
    () => bodyLogParams().bodyRetentionMode,
    (mode) => {
      bodyLogMode.value = mode
    },
    { immediate: true },
  )

  // 首次进入拉取回显,load 完成后把服务端值回填进三个表单
  void settingsStore.load()
  watch(
    () => settingsStore.settings,
    (settings) => {
      if (!settings) return
      setValues(toGeneralModel(settings))
      setMaintenanceValues(toMaintenanceModel(settings))
      setBodyLogValues(toBodyLogModel(settings))
    },
  )

  return { LmForm, MaintenanceForm, BodyLogForm }
}
