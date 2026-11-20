// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
vi.mock('@/api/deployment', async (importOriginal) => ({
  // normalizedTopology 是纯函数,直接用真实实现(拓扑双写归一取数)
  ...(await importOriginal<typeof import('@/api/deployment')>()),
  inspectDeployment: vi.fn(),
}))
vi.mock('@/api/inference-image', () => ({
  listInferenceEngines: vi.fn(),
  listInferenceImages: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
vi.mock('@/views/deploy/components/deploy-list/useStore', () => ({ useStore: vi.fn() }))

import { inspectDeployment } from '@/api/deployment'
import { listInferenceEngines, listInferenceImages } from '@/api/inference-image'
import type { InspectDeploymentResult } from '@/api/deployment'
import type { ImageEntryOut } from '@/api/inference-image'
import { useStore as useDeployListStore } from '@/views/deploy/components/deploy-list/useStore'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import StepBasic from '@/views/deploy/components/deploy-list/create-wizard/steps/StepBasic.vue'

const store = useWizardStore()

function makeInspect(overrides: Partial<InspectDeploymentResult> = {}): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 3,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:0.9.5',
      topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

function makeImageEntry(overrides: Partial<ImageEntryOut> = {}): ImageEntryOut {
  return {
    id: 'img-1',
    engine: 'vllm',
    arch: 'nvidia',
    gpu_model: ['*'],
    driver: null,
    engine_version: '0.9.5',
    image: 'reg/vllm:0.9.5',
    default: false,
    notes: '',
    ...overrides,
  } as ImageEntryOut
}

function nameInput(): HTMLInputElement {
  const input = [...document.querySelectorAll<HTMLInputElement>('input')].find((item) =>
    item.placeholder.includes('默认按模型名自动生成'),
  )
  expect(input, '部署名称输入框').toBeTruthy()
  return input!
}

function summaryText(): string {
  return document.querySelector('.deploy-cluster-summary')?.textContent ?? ''
}

/** Step1 是否渲染镜像地址输入框(输入框已移到 Step3「高级参数」,此处恒为 false) */
function hasImageInput(): boolean {
  return [...document.querySelectorAll<HTMLInputElement>('input')].some((item) =>
    item.placeholder.includes('vllm-ascend'),
  )
}

async function settle() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useDeployListStore).mockReturnValue({ createDeploy: vi.fn() } as unknown as ReturnType<typeof useDeployListStore>)
  store.reset()
  // 预置:模型选项/集群选项/引擎字典(选定模型会按 model_id 重拉字典 → mock 同源返回)
  store.modelOptions.value = [{ label: 'Qwen2.5-7B', value: 7, raw: {} }] as typeof store.modelOptions.value
  store.clusterOptions.value = [{ label: 'gpu-shared(2节点)', value: 3, raw: {} }] as typeof store.clusterOptions.value
  const engines = [
    { engine: 'vllm', display_name: 'vLLM', deployable: true },
  ] as unknown as typeof store.engines.value
  store.engines.value = engines
  vi.mocked(listInferenceEngines).mockResolvedValue(engines)
  vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
  vi.mocked(listInferenceImages).mockResolvedValue([
    makeImageEntry({ engine_version: '0.9.5', image: 'reg/vllm:0.9.5' }),
    makeImageEntry({ id: 'img-2', engine_version: '0.9.4', image: 'reg/vllm:0.9.4', default: true }),
  ])
})

describe('StepBasic 部署名称自动生成(c0f07d9)', () => {
  it('挂载时已有模型(quick-setup 预填):按模型名生成 slug + 4 位后缀', async () => {
    store.form.modelId = 7
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()

    expect(nameInput().value).toMatch(/^qwen2-5-7b-[a-z0-9]{4}$/)
    wrapper.unmount()
  })

  it('用户手输名称后,模型选项迟到不覆盖手输', async () => {
    // 选项迟到:挂载时为空,modelId 已有
    store.form.modelId = 7
    store.modelOptions.value = []
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()
    expect(nameInput().value).toBe('')

    // 用户先输入
    const input = nameInput()
    input.value = 'my-custom-deploy'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()

    // 选项迟到补齐:不覆盖手输
    store.modelOptions.value = [{ label: 'Qwen2.5-7B', value: 7, raw: {} }] as typeof store.modelOptions.value
    await settle()
    expect(nameInput().value).toBe('my-custom-deploy')
    wrapper.unmount()
  })

  it('名称为空时,模型选项迟到自动补齐', async () => {
    store.form.modelId = 7
    store.modelOptions.value = []
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()

    store.modelOptions.value = [{ label: 'Qwen2.5-7B', value: 7, raw: {} }] as typeof store.modelOptions.value
    await settle()
    expect(nameInput().value).toMatch(/^qwen2-5-7b-[a-z0-9]{4}$/)
    wrapper.unmount()
  })
})

describe('StepBasic 推荐回填与镜像版本联动(f8cac24)', () => {
  it('推荐回填集群/引擎,validate 后写回 store.form;最大上下文不再回填(移至 Step2)', async () => {
    store.form.modelId = 7
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()

    // validate 通过(名称自动生成 + 集群推荐回填)并同步 store.form
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    await vi.waitFor(() => expect(summaryText()).toContain('集群规模'))
    // 镜像自动预填不再展示在概要里,以 store.autoImage(自动值基线)为准
    await vi.waitFor(() => expect(store.autoImage.value).toBe('reg/vllm:0.9.5'))

    const ok = await validate()
    expect(ok).toBe(true)
    expect(store.form.clusterId).toBe(3)
    // 最大上下文在 Step2 编辑:Step1 不回填,保持默认 8K
    expect(store.form.maxModelLen).toBe(8 * 1024)
    expect(store.form.name).toMatch(/^qwen2-5-7b-/)
    // 推荐预检只按模型请求,不再携带上下文
    expect(vi.mocked(inspectDeployment).mock.calls[0][0]).toEqual({ model_id: 7 })
    wrapper.unmount()
  })

  it('镜像版本默认回填:recommendation.image 精确匹配优先', async () => {
    store.form.modelId = 7
    const wrapper = mount(StepBasic, { attachTo: document.body })
    // 镜像目录加载完成才回填版本(推荐镜像命中 0.9.5 变体)
    await vi.waitFor(() => expect(store.imageEntries.value.length).toBeGreaterThan(0))
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.imageTag).toBe('0.9.5')
    wrapper.unmount()
  })

  it('镜像版本回退:无精确匹配时取 catalog 默认版本', async () => {
    vi.mocked(listInferenceImages).mockResolvedValue([
      makeImageEntry({ id: 'img-2', engine_version: '0.9.4', image: 'reg/vllm:0.9.4', default: true }),
    ])
    store.form.modelId = 7
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await vi.waitFor(() => expect(store.imageEntries.value.length).toBeGreaterThan(0))
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.imageTag).toBe('0.9.4')
    wrapper.unmount()
  })

  it('无模型/推荐时镜像版本禁用,占位提示先选模型与引擎', async () => {
    // 全新表单:无 modelId,无 inspect 推荐
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()
    expect(document.body.textContent).toContain('请先选择模型与引擎')
    wrapper.unmount()
  })

  it('必填校验:缺模型与集群时 validate 返回 false 且不写回', async () => {
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    const ok = await validate()
    expect(ok).toBe(false)
    expect(store.form.modelId).toBeNull()
    expect(store.form.clusterId).toBeNull()
    wrapper.unmount()
  })

  it('副本数/最大上下文表单项已移除,validate 写回保留 store 既有值', async () => {
    store.form.modelId = 7
    store.form.replicas = 2
    store.form.maxModelLen = 4096
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()
    await vi.waitFor(() => expect(summaryText()).toContain('集群规模'))

    // 两个输入不再渲染,概要提示引导到「选择算力」
    expect(document.body.textContent).not.toContain('留空按模型默认上下文')
    expect(document.body.textContent).toContain('在下一步「选择算力」中确定')

    // 未注册表单项的字段经 params 原样回传,store 既有值不丢
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.replicas).toBe(2)
    expect(store.form.maxModelLen).toBe(4096)
    wrapper.unmount()
  })
})

describe('StepBasic 镜像地址自动预填(输入框已移至 Step3 高级参数)', () => {
  it('按预检解析结果自动预填,记自动值基线;Step1 不再渲染镜像地址与「将使用镜像」', async () => {
    store.form.modelId = 7
    const wrapper = mount(StepBasic, { attachTo: document.body })
    // 预填 = 推荐镜像(resolved 缺失时的兜底),同时记为自动值基线
    await vi.waitFor(() => expect(store.autoImage.value).toBe('reg/vllm:0.9.5'))
    expect(hasImageInput()).toBe(false)
    expect(summaryText()).not.toContain('将使用镜像')

    // 预填值随 validate 写回 store,供 Step3「高级参数」回显
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.image).toBe('reg/vllm:0.9.5')
    wrapper.unmount()
  })

  it('手选镜像版本后,Step1 期间的基础预检不得让后续 inspect 带上旧推荐 image', async () => {
    store.form.modelId = 7
    vi.mocked(inspectDeployment)
      // 选模型即推荐:推荐镜像地址不在 catalog 里 → 版本回填到 catalog 默认版本 0.9.4
      // (等价于用户手选了与推荐不同的镜像版本)
      .mockResolvedValueOnce(
        makeInspect({
          recommendation: { ...makeInspect().recommendation, image: 'reg/vllm:9.9.9' },
        }),
      )
      // Step1→2 基础预检:后端按已选版本 0.9.4 解析出实际镜像
      .mockResolvedValueOnce(
        makeInspect({
          recommendation: { ...makeInspect().recommendation, image: 'reg/vllm:0.9.4' },
        }),
      )
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await vi.waitFor(() => expect(store.autoImage.value).toBe('reg/vllm:9.9.9'))
    await vi.waitFor(() => expect(store.imageEntries.value.length).toBeGreaterThan(0))
    await settle()

    // Step1 写回表单:镜像地址=推荐值(autoImage 同值),镜像版本=catalog 默认 0.9.4
    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.imageTag).toBe('0.9.4')
    expect(store.imageOverride.value).toBe('')

    // controller.next() 在切步前 await 基础预检 → 此刻 StepBasic 仍挂载,预填 watch 仍在生效
    await store.loadInspect()
    await settle()

    expect(vi.mocked(inspectDeployment).mock.calls.at(-1)?.[0].image_tag).toBe('0.9.4')
    // 预检解析出手选版本的实际镜像后,自动基线跟随:旧推荐值不得被当成用户手改(否则 image 盖掉 image_tag)
    expect(store.form.image).toBe('reg/vllm:0.9.4')
    expect(store.imageOverride.value).toBe('')
    wrapper.unmount()
  })

  it('手填地址不被迟到的推荐覆盖,validate 写回手填值', async () => {
    store.form.modelId = 7
    // 集群先手动选定:本用例只验证「迟到不覆盖」,不依赖推荐回填
    store.form.clusterId = 3
    // 用户在 Step3 高级参数里手填过地址(autoImage 为空 → 判定为手改)
    store.form.image = 'xxx.io/xxa/vllm-ascend:0.313'
    // 预检迟到:首次推荐请求失败(store.inspect 仍为空 → 无自动预填)
    vi.mocked(inspectDeployment).mockRejectedValueOnce(new Error('pending'))
    const wrapper = mount(StepBasic, { attachTo: document.body })
    await settle()

    // 推荐迟到补齐:不覆盖手填值(自动值基线仍为空 → 未发生预填)
    store.inspect.value = makeInspect()
    await settle()
    expect(store.autoImage.value).toBe('')

    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    expect(await validate()).toBe(true)
    expect(store.form.image).toBe('xxx.io/xxa/vllm-ascend:0.313')
    wrapper.unmount()
  })
})
