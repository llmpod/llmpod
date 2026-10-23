<script setup lang="ts">
import { h } from 'vue'
import { FormCustom, FormInput, FormSelect, useForm } from '@repo/hooks'
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
    // 集群概要:跟随所选集群联动,展示真实派生统计
    FormCustom(({ model }) => {
      const cluster = store.clusterOptions.value.find((item) => item.value === model.clusterId)?.raw
      // 镜像:选了版本取该版本的默认变体 ref;未选回退 inspect 粗镜像(recommendation.image)
      const image = model.imageTag
        ? (store.imageEntries.value.find(
            (entry) => entry.engine_version === model.imageTag && entry.default,
          ) ?? store.imageEntries.value.find((entry) => entry.engine_version === model.imageTag))
            ?.image
        : store.inspect.value?.recommendation?.image
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
        h('div', { class: 'deploy-cluster-summary__row' }, [
          h('span', { class: 'deploy-cluster-summary__label' }, '将使用镜像:'),
          image || '选择模型后按推荐解析',
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

/** 镜像版本下拉选项:按 engine_version 去重(catalog 同版本多 gpu_model/driver 变体合并为一项) */
const imageVersionOptions = computed<{ label: string; value: string }[]>(() => {
  const seen = new Map<string, { label: string; value: string }>()
  for (const entry of store.imageEntries.value) {
    if (!entry.engine_version || seen.has(entry.engine_version)) continue
    seen.set(entry.engine_version, {
      value: entry.engine_version,
      label: entry.notes ? `${entry.engine_version}(${entry.notes})` : entry.engine_version,
    })
  }
  return [...seen.values()]
})

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
