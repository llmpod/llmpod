<script setup lang="ts">
import { h, type VNodeChild } from 'vue'
import { NTooltip } from 'naive-ui'
import { FormCustom, FormInput, FormSelect, useForm } from '@repo/hooks'
import { buildImageVersionOptions, formatDriver, type ImageVersionOption } from '../imageVersion'
import { useWizardStore, type WizardFormValues } from '../useStore'

const store = useWizardStore()

const formApi = useForm<WizardFormValues>({
  // 从向导 store 回读:组件被 Transition 卸载后重挂载,表单值跨步骤留存
  data: () => ({ ...store.form }),
  options: () => [
    FormSelect('模型', 'modelId', store.modelOptions.value, {
      // number 值需显式 type:'number',否则 required 按 string 校验恒失败
      rules: [
        { required: true, type: 'number', message: '请选择模型', trigger: ['blur', 'change'] },
      ],
      attrs: { loading: store.modelLoading.value, placeholder: '选择已上架的模型' },
    }),
    FormInput('部署名称', 'name', {
      rules: [{ required: true, message: '请输入部署名称', trigger: 'blur' }],
      attrs: { placeholder: '默认按模型名自动生成,可修改' },
    }),
    FormSelect('推理引擎', 'engine', engineOptions.value, {
      attrs: { clearable: false, loading: store.enginesLoading.value },
    }),
    // 镜像版本:随生效引擎联动加载,留空由后端 catalog 按默认版本解析
    FormSelect('镜像版本', 'imageTag', imageVersionOptions.value, {
      attrs: {
        clearable: true,
        loading: store.imageEntriesLoading.value,
        disabled: !effectiveFormEngine.value,
        placeholder: effectiveFormEngine.value ? '留空按后端默认版本' : '请先选择模型与引擎',
        // 一行一版本:版本号 + 架构标签 + 末尾「驱动要求」(悬浮看各架构驱动下限);下拉项与已选项共用同一渲染
        renderLabel: (option: ImageVersionOption) => renderVersionLabel(option),
      },
    }),
    FormSelect('目标集群', 'clusterId', store.clusterOptions.value, {
      // number 值需显式 type:'number',否则 required 按 string 校验恒失败
      rules: [
        { required: true, type: 'number', message: '请选择目标集群', trigger: ['blur', 'change'] },
      ],
      attrs: {
        clearable: false,
        loading: store.clusterLoading.value,
        placeholder: '选择要部署到的集群',
      },
    }),
    // 集群概要:跟随所选集群联动,展示真实派生统计(镜像不再在此展示,见 Step3「高级参数」)
    FormCustom(({ model }) => {
      const cluster = store.clusterOptions.value.find((item) => item.value === model.clusterId)?.raw
      return h('div', { class: 'deploy-cluster-summary' }, [
        h('div', { class: 'deploy-cluster-summary__row' }, [
          h('span', { class: 'deploy-cluster-summary__label' }, '集群规模:'),
          cluster
            ? `节点 ${cluster.online_node_count}/${cluster.node_count} 在线 · GPU 共 ${cluster.gpu_count} 张`
            : '请选择集群后展示概要',
        ]),
        h('div', { class: 'deploy-cluster-summary__row' }, [
          h('span', { class: 'deploy-cluster-summary__label' }, '副本规模:'),
          '副本数与每副本卡数在下一步「选择算力」中确定',
        ]),
      ])
    }),
  ],
  layout: { labelPlacement: 'top' },
})

const { LmForm, validate } = formApi
// 显式标注打破循环推导:options 闭包引用 engineOptions、engineOptions 又引用 params,直接解构会被 TS 判为 any
const params: () => WizardFormValues = formApi.params

/**
 * 引擎下拉选项:从能力字典按当前所选模型的类别/格式本地过滤(声明在 useForm 后避免类型循环推导,渲染期才求值)。
 * 模型未选时展示全部可部署引擎;字典未加载时仅剩「自动选择」。
 */
const engineOptions = computed<{ label: string; value: string; disabled?: boolean }[]>(() =>
  store.engineOptionsFor(),
)

/** Step1 生效引擎:手动值优先,auto 取 inspect 推荐(镜像版本下拉以此为准) */
const effectiveFormEngine = computed(() =>
  params().engine !== 'auto' ? params().engine : store.inspect.value?.recommendation?.engine,
)

/** 镜像版本下拉项:同名版本的多条 catalog 变体(按 arch/gpu_model/driver 区分)聚合后的一行 */
const imageVersionOptions = computed(() => buildImageVersionOptions(store.imageEntries.value))

/**
 * 架构标签配色:nvidia 绿 / cann 橙(与蓝、青明显区分,两种色相互补不易看混)。
 * 直接消费运行时主题色变量(--n-success-color / --n-warning-color 是 RGB 串,须用 rgb()/rgba() 包裹),
 * 字取实色、底取 10% 透明,明暗主题与主题色变更都自动跟随。
 */
const ARCH_TAG_STYLE: Record<string, { background: string; color: string }> = {
  nvidia: {
    background: 'rgba(var(--n-success-color), 0.1)',
    color: 'rgb(var(--n-success-color))',
  },
  cann: {
    background: 'rgba(var(--n-warning-color), 0.12)',
    color: 'rgb(var(--n-warning-color))',
  },
}

/** 架构标签(VNode):彩色小 tag,文案为 arch 原值 */
function renderArchTag(arch: string): VNodeChild {
  const style = ARCH_TAG_STYLE[arch]
  return h(
    'span',
    {
      key: arch,
      class: 'shrink-0 text-base leading-[1.6] px-[.6rem] rounded-[.3rem] whitespace-nowrap',
      style: style
        ? { backgroundColor: style.background, color: style.color }
        : { backgroundColor: 'var(--c-fill-4)', color: 'var(--c-secondary)' },
    },
    arch,
  )
}

/**
 * 「驱动要求」入口:靠右显示,默认隐藏,仅鼠标悬浮到本选项行时出现(样式见下方全局块)。
 * 无下划线/虚线,纯文字 + 悬浮浮层(浮层内是按架构分组的驱动下限)。
 */
function renderDriverRequirement(option: ImageVersionOption): VNodeChild {
  const trigger = h('span', { class: 'cursor-help whitespace-nowrap' }, '驱动要求')
  // 无驱动信息时不渲染入口(不留空位)
  if (!option.drivers.length) return null
  return h('span', { class: 'deploy-image-version__driver ml-auto shrink-0' }, [
    h(
      NTooltip,
      { trigger: 'hover', placement: 'right' },
      {
        trigger: () => trigger,
        default: () =>
          h(
            'div',
            { class: 'flex flex-col gap-[.4rem] min-w-[16rem]' },
            option.drivers.map((row) =>
              h(
                'div',
                {
                  key: `${row.group}|${row.driver}`,
                  class: 'flex justify-between gap-[2.4rem] tabular-nums',
                },
                [
                  h('span', { class: 'text-secondary' }, row.group),
                  h('span', null, formatDriver(row.driver)),
                ],
              ),
            ),
          ),
      },
    ),
  ])
}

/** 版本下拉项渲染:版本号 + 架构彩色标签 + 悬浮显示的「驱动要求」;下拉项与已选回显共用 */
function renderVersionLabel(option: ImageVersionOption): VNodeChild {
  // w-full:让行撑满选项内容区,舍此 ml-auto 只能把「驱动要求」推到文字宽度处(紧贴 tag)
  return h('div', { class: 'deploy-image-version flex items-center gap-[.6rem] min-w-0 w-full' }, [
    h('span', { class: 'shrink-0' }, option.label),
    ...option.archs.map((arch) => renderArchTag(arch)),
    renderDriverRequirement(option),
  ])
}

/** 平台解析出的镜像:三层解析回显优先,进 Step2 前退回推荐镜像(镜像地址框的自动预填源) */
const resolvedImage = computed(
  () => store.inspect.value?.resolved?.image ?? store.inspect.value?.recommendation?.image ?? '',
)

// 自动预填镜像地址:字段为空或仍是上次自动值时覆盖,保留用户手改;仅改动过才作为 spec.image 提交。
// 输入框已移到 Step3「高级参数」,此处只负责把预检解析结果写进表单值(store.autoImage 记自动值基线)。
watch(
  resolvedImage,
  (image) => {
    if (!image) return
    const model = params()
    if (model.image.trim() && model.image !== store.autoImage.value) return
    model.image = image
    store.autoImage.value = image
  },
  { immediate: true },
)

const alertIcon = (() => {
  const src = `${import.meta.env.BASE_URL}icons/deploy-list/info.svg`
  return {
    backgroundColor: 'currentColor',
    width: '1.4rem',
    height: '1.4rem',
    mask: `url(${src}) center/contain no-repeat`,
    WebkitMask: `url(${src}) center/contain no-repeat`,
  }
})()

/** 上次自动生成的部署名称:区分用户手输,自动值只覆盖空值/上次自动值,不覆盖手输 */
let autoName: string | null = null

/** 4 位随机后缀(小写字母+数字,保持部署名风格合法) */
function randomSuffix(): string {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
  return Array.from({ length: 4 }, () => chars[Math.floor(Math.random() * chars.length)]).join('')
}

// 选定模型即按「模型名-4位随机数字字母」生成部署名称(模型名规整为小写连字符风格,如 Qwen2.5-7B → qwen2-5-7b-a1b2);
// 依赖 modelOptions 取模型名:列表晚于 modelId 就绪(如 quick-setup 预填)时加载完成后会再触发一次补齐
watch(
  () => [params().modelId, store.modelOptions.value] as const,
  ([modelId]) => {
    if (modelId === null) return
    const modelName = store.modelOptions.value.find((item) => item.value === modelId)?.label
    if (!modelName) return
    const model = params()
    // 字段为空或仍是上次自动值时才覆盖,保留用户手输
    if (model.name && model.name !== autoName) return
    const slug = modelName
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
    autoName = `${slug || 'deploy'}-${randomSuffix()}`
    model.name = autoName
  },
  { immediate: true },
)

// 选定模型即请求推荐,按返回自动回填推理引擎/目标集群;失败静默(HTTP 层已提示),可手选。
// 最大上下文已移至 Step2「选择算力」编辑:调整后走全量预检重估显存,推荐值由派生拓扑回退兜底。
// immediate:挂载时已有模型(如 quick-setup 预填)也触发一次推荐;正常新建流程初始 modelId 为 null 直接早退
watch(
  () => params().modelId,
  async (modelId) => {
    if (modelId === null) return
    // 引擎字典按模型判定支持性(不支持的引擎置灰):模型变了必须重拉
    store.loadEngines(modelId).catch(() => {})
    const result = await store.inspectRecommendation(modelId)
    // 竞态守卫:请求期间又切换了模型则丢弃本次回填
    if (!result || params().modelId !== modelId) return
    const model = params()
    // 推荐集群须在 active 选项内才回填
    if (
      result.recommended_cluster_id !== null &&
      store.clusterOptions.value.some((item) => item.value === result.recommended_cluster_id)
    ) {
      model.clusterId = result.recommended_cluster_id
    }
    const engine = result.recommendation.engine
    const option = engineOptions.value.find((item) => item.value === engine)
    if (option && !option.disabled) model.engine = engine
  },
  { immediate: true },
)

// 可选集合收窄(切换模型/字典加载完成)后,已选引擎不再可选(或被按模型判为不支持)时回退「自动选择」;
// 字典迟到(inspect 推荐先于字典返回)时补偿回填:仍为自动选择且推荐引擎可选用则补上
watch(engineOptions, (options) => {
  const model = params()
  const current = options.find((item) => item.value === model.engine)
  if (!current || current.disabled) model.engine = 'auto'
  const recommended = store.inspect.value?.recommendation?.engine
  if (
    model.engine === 'auto' &&
    recommended &&
    options.some((item) => item.value === recommended && !item.disabled)
  ) {
    model.engine = recommended
  }
})

// 生效引擎变化即拉镜像目录;默认选中 recommendation.image 所在版本,无精确匹配退到 catalog 默认版本
watch(effectiveFormEngine, async (engine, prevEngine) => {
  const model = params()
  if (!engine) {
    model.imageTag = null
    return
  }
  await store.loadImageVersions(engine)
  // 竞态守卫:加载期间引擎又切换则丢弃本次默认回填(store 内请求序号已保证只认最后响应)
  if (effectiveFormEngine.value !== engine) return
  // 同引擎重复触发且手选版本仍在列表时保留(不覆盖用户手选)
  if (
    engine === prevEngine &&
    imageVersionOptions.value.some((item) => item.value === model.imageTag)
  ) {
    return
  }
  const entries = store.imageEntries.value
  const recommendedImage = store.inspect.value?.recommendation?.image
  model.imageTag =
    entries.find((entry) => entry.image === recommendedImage)?.engine_version ??
    entries.find((entry) => entry.default)?.engine_version ??
    null
})

defineExpose({
  /** 下一步门控:表单校验通过后写回向导 store(集群变化时 store 内清拓扑缓存与已选 GPU) */
  validate: async () => {
    try {
      await validate()
    } catch {
      return false
    }
    store.applyForm({ ...params() })
    return true
  },
})
</script>

<template>
  <div class="flex flex-col gap-[.75rem] w-full max-w-[56rem] mx-auto">
    <!-- 限宽居中:避免大屏下表单被拉得过长 -->
    <LmAlert type="primary">
      <template #icon>
        <span :style="alertIcon" />
      </template>
      选择模型后按部署预检自动推荐推理引擎与目标集群,也可手动调整;副本数与最大上下文在下一步「选择算力」中设置
    </LmAlert>
    <LmForm />
  </div>
</template>

<style scoped>
/* 集群概要块(FormCustom 渲染在表单组件内,需 :deep 穿透 scoped) */
:deep(.deploy-cluster-summary) {
  padding: 1rem 1.2rem;
  border-radius: 0.6rem;
  font-size: 1.2rem;
  background-color: var(--c-fill-4);
}

:deep(.deploy-cluster-summary__row) {
  display: flex;
  gap: 0.8rem;
  line-height: 1.8;
  color: var(--c-default);
}

:deep(.deploy-cluster-summary__label) {
  flex-shrink: 0;
  width: 8rem;
  text-align: right;
  color: var(--c-secondary);
}
</style>

<style>
/*
 * 镜像版本下拉:下拉菜单由 naive-ui teleport 到 body,scoped 命中不到,
 * 故「驱动要求」的显隐用全局块 + 专属 class 前缀(同 deploy-detail/index.vue 的画布样式做法)。
 *
 * 显示口径:
 * - 输入框回显(已选版本,.n-base-selection 内):常驻显示,右侧靠边,方便随时确认该版本的驱动要求;
 * - 下拉选项行:不常驻,只有鼠标悬浮到该行时才在行右端出现。
 */
.deploy-image-version__driver {
  display: none;
  color: var(--c-primary);
  font-size: 1.2rem;
}

/* 选项内容区默认是 block 且宽度贴合文字,这里撑满整行,「驱动要求」才能真正靠右 */
.n-base-select-option__content:has(> .deploy-image-version) {
  width: 100%;
}

/* 已选回显(输入框内)常驻显示 */
.n-base-selection .deploy-image-version__driver {
  display: inline-flex;
  align-items: center;
}

/*
 * 下拉选项行:只有真实鼠标悬浮时才显示。
 * 不用 --pending:naive-ui 用它记录「鼠标最后停留/键盘高亮」的项且一直保留,
 * 会让选中过的版本重开下拉后不悬浮也显示。
 */
.n-base-select-option:hover > .n-base-select-option__content > .deploy-image-version > .deploy-image-version__driver {
  display: inline-flex;
  align-items: center;
}
</style>
