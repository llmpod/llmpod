import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
vi.mock('@/api/deployment', () => ({ inspectDeployment: vi.fn() }))
vi.mock('@/api/inference-image', () => ({
  listInferenceEngines: vi.fn(),
  listInferenceImages: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
// 父级列表 store:提交创建只断言透传的 payload,不引入真实实现
vi.mock('@/views/deploy/components/deploy-list/useStore', () => ({ useStore: vi.fn() }))

import { inspectDeployment } from '@/api/deployment'
import { useStore as useDeployListStore } from '@/views/deploy/components/deploy-list/useStore'
import type { ClusterTopology, TopologyGpu, TopologyNode } from '@/api/topology'
import type { EngineOut } from '@/api/inference-image'
import type { InspectDeploymentResult } from '@/api/deployment'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'

const createDeploy = vi.fn()
// useWizardStore() 在模块顶层调用并捕获 deployStore,mock 返回值必须先就绪
vi.mocked(useDeployListStore).mockReturnValue({
  createDeploy,
} as unknown as ReturnType<typeof useDeployListStore>)
const store = useWizardStore()

const PLATFORM_COMMAND =
  'vllm serve --model {model_path} --served-model-name {served_model_name} --host 0.0.0.0 --port {port} --tensor-parallel-size {tp}'

function makeNode(id: string): TopologyNode {
  return { id, cluster_id: 1, hostname: id, ip: '127.0.0.1', status: 'online' } as TopologyNode
}

function makeGpu(uuid: string, nodeId: string): TopologyGpu {
  return {
    id: 1,
    uuid,
    node_id: nodeId,
    index: 0,
    name: 'GPU',
    arch: 'nvidia',
    total_vram_gb: 80,
    available_vram_gb: 80,
    util: 0,
    status: 'online',
  } as TopologyGpu
}

function makeInspect(overrides: Partial<InspectDeploymentResult> = {}): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 3,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:0.9.5',
      topology: { tp: 2, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

/** 全量预检回显:args 用于逐行区,command 是平台反推的命令模板 */
function resolvedWith(command = PLATFORM_COMMAND): InspectDeploymentResult {
  return makeInspect({
    resolved: {
      image: 'reg/vllm:0.9.5',
      args: ['--model', '{model_path}', '--port', '{port}', '--tensor-parallel-size', '2'],
      env: {},
      protected_overlap: [],
      command,
    },
  })
}

function seed() {
  store.form.name = 'dep-cmd'
  store.form.modelId = 7
  store.form.clusterId = 3
  store.form.engine = 'vllm'
  store.form.maxModelLen = null
  store.topology.value = {
    cluster: { id: 3, name: 'c3', type: 'docker', status: 'active' },
    nodes: [makeNode('n1')],
    gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1')],
    deployments: [],
  } as ClusterTopology
  store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
  store.inspect.value = makeInspect()
  store.engines.value = [
    { engine: 'vllm', display_name: 'vLLM', deployable: true },
  ] as unknown as EngineOut[]
}

/** 进 Step3 前的全量预检:回填命令基线与逐行参数 */
async function enterConfirmStep(command = PLATFORM_COMMAND) {
  vi.mocked(inspectDeployment).mockResolvedValue(resolvedWith(command))
  await store.runFinalInspect()
}

beforeEach(() => {
  vi.clearAllMocks()
  createDeploy.mockReset()
  vi.mocked(useDeployListStore).mockReturnValue({
    createDeploy,
  } as unknown as ReturnType<typeof useDeployListStore>)
  store.reset()
  seed()
})

describe('命令模板回填与 dirty 守卫', () => {
  it('未手改时按预检 resolved.command 回填文本与基线', async () => {
    await enterConfirmStep()
    expect(store.editCommand.value).toBe(PLATFORM_COMMAND)
    expect(store.platformCommand.value).toBe(PLATFORM_COMMAND)
    expect(store.commandDirty.value).toBe(false)
    expect(store.commandActive.value).toBe(true)
    expect(store.commandLocked.value).toBe(false)
  })

  it('手改后再次预检只更新基线,不覆盖用户文本;重置可显式同步', async () => {
    await enterConfirmStep()
    const edited = 'vllm serve --model {model_path} --port {port} --max-model-len 4096'
    store.editCommand.value = edited
    store.markCommandDirty()

    const nextCommand = `${PLATFORM_COMMAND} --max-model-len 4096`
    await enterConfirmStep(nextCommand)
    expect(store.editCommand.value).toBe(edited)
    expect(store.platformCommand.value).toBe(nextCommand)

    store.resetCommand()
    expect(store.editCommand.value).toBe(nextCommand)
    expect(store.commandDirty.value).toBe(false)
  })

  it('清空命令 = 回退平台自动生成:参数区锁定并给出提示', async () => {
    await enterConfirmStep()
    store.editCommand.value = ''
    store.markCommandDirty()
    expect(store.commandActive.value).toBe(false)
    expect(store.commandLocked.value).toBe(true)
    expect(store.commandLockReason.value).toContain('已回退为平台自动生成')
  })
})

describe('逐行参数单向写入命令', () => {
  it('逐行参数改动按 flag 名写进命令;改名时旧 flag 一并移除', async () => {
    await enterConfirmStep()
    store.addArg()
    store.setArg(3, '--max-model-len 4096')
    expect(store.editCommand.value).toContain('--max-model-len 4096')
    expect(store.commandDirty.value).toBe(true)

    // 改名:旧 flag 不得残留
    store.setArg(3, '--max-num-seqs 32')
    expect(store.editCommand.value).not.toContain('--max-model-len')
    expect(store.editCommand.value).toContain('--max-num-seqs 32')
  })

  it('已有同名 param 时替换值而不是重复追加', async () => {
    await enterConfirmStep()
    store.setArg(1, '--port 9000')
    expect(store.editCommand.value).toContain('--port 9000')
    expect(store.editCommand.value.match(/--port/g)).toHaveLength(1)
  })

  it('删除参数行同时把该 flag 从命令里摘掉', async () => {
    await enterConfirmStep()
    store.removeArg(2) // --tensor-parallel-size 2
    expect(store.editCommand.value).not.toContain('--tensor-parallel-size')
  })

  it('系统必控行(`--model`)不可删,误触发删除也不会动命令', async () => {
    // 预检拓扑把 --model 声明为必控项:该行只读,删除动作不得落到命令里
    vi.mocked(inspectDeployment).mockResolvedValue({
      ...resolvedWith(),
      preview_topology: { engines: [{ engine: 'vllm', protected_flags: ['--model'] }] },
    } as unknown as InspectDeploymentResult)
    await store.runFinalInspect()
    expect(store.isArgProtected('--model {model_path}')).toBe(true)

    store.removeArg(0) // --model {model_path}
    expect(store.editCommand.value).toContain('--model {model_path}')
  })

  it('锁定态拒绝写入(含 shell 语法 / 自定义入口)', async () => {
    await enterConfirmStep()
    store.editCommand.value = `source /usr/local/Ascend/ascend-toolkit/set_env.sh && exec ${PLATFORM_COMMAND}`
    store.markCommandDirty()
    expect(store.commandLocked.value).toBe(true)
    expect(store.commandLockReason.value).toContain('shell')

    const locked = store.editCommand.value
    store.addArg()
    store.setArg(3, '--max-model-len 4096')
    expect(store.editCommand.value).toBe(locked)

    // 自定义入口(myvllm xxxx):镜像入口不再执行,逐行区同样禁用
    store.editCommand.value = 'myvllm serve --port {port}'
    expect(store.commandLocked.value).toBe(true)
    expect(store.commandLockReason.value).toContain('自定义命令模式')
  })
})

describe('命令校验', () => {
  it('缺 {port} 为本地阻断项,并把当前命令回传给 inspect', async () => {
    await enterConfirmStep()
    store.editCommand.value = 'vllm serve --model {model_path}'
    store.markCommandDirty()
    const { blocking } = await store.validateCommand()

    expect(blocking).toHaveLength(1)
    expect(blocking[0]).toContain('{port}')
    const payload = vi.mocked(inspectDeployment).mock.calls.at(-1)?.[0]
    expect(payload?.command).toBe('vllm serve --model {model_path}')
  })

  it('合并后端 issues,且不覆盖用户已手改的文本', async () => {
    await enterConfirmStep()
    store.editCommand.value = 'vllm serve --model {model_path} --port {port}'
    store.markCommandDirty()
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect({ issues: ['显存余量不足 10%'] }))

    const { blocking, issues } = await store.validateCommand()
    expect(blocking).toEqual([])
    expect(issues).toEqual(['显存余量不足 10%'])
    expect(store.commandIssues.value).toEqual(['显存余量不足 10%'])
    expect(store.editCommand.value).toBe('vllm serve --model {model_path} --port {port}')
  })

  it('命令为空时清空校验结果且不发请求', async () => {
    await enterConfirmStep()
    const calls = vi.mocked(inspectDeployment).mock.calls.length
    store.editCommand.value = ''
    const { issues } = await store.validateCommand()
    expect(issues).toEqual([])
    expect(vi.mocked(inspectDeployment).mock.calls).toHaveLength(calls)
  })
})

describe('提交 payload:命令模式 vs 平台自动生成', () => {
  it('命令非空时只发 spec.command,不发 extra_args;镜像 tag 仍可用于选镜像', async () => {
    seed()
    store.form.imageTag = '0.9.5'
    await enterConfirmStep()
    // 逐行区加一条参数(已写进命令):整条接管,不再回传 extra_args
    store.addArg()
    store.setArg(3, '--max-model-len 4096')
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.command).toContain('vllm serve --model {model_path}')
    expect(spec?.command).toContain('--max-model-len 4096')
    expect(spec?.engine_params?.extra_args).toBeUndefined()
    // spec.image_tag 仍可用于选镜像(命令模式只接管 args/env,不接管镜像版本)
    expect(spec?.image_tag).toBe('0.9.5')
    expect(spec?.image).toBeUndefined()
  })

  it('命令清空后回退老路径:差集口径的 extra_args 照旧回传', async () => {
    await enterConfirmStep()
    store.editCommand.value = ''
    store.addArg()
    store.setArg(3, '--max-model-len 4096')
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.command).toBeUndefined()
    expect(spec?.engine_params?.extra_args).toEqual(['--max-model-len', '4096'])
  })

  it('镜像地址:仅用户改动过才作为 spec.image 提交,未改仍走 image_tag', async () => {
    seed()
    store.form.imageTag = '0.9.5'
    // 自动预填未改动 → 不覆盖 catalog 变体裁决
    store.autoImage.value = 'reg/vllm:0.9.5'
    store.form.image = 'reg/vllm:0.9.5'
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })
    await store.submitCreate()
    let spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.image).toBeUndefined()
    expect(spec?.image_tag).toBe('0.9.5')

    // 手填完整地址 → image 优先,不再传 image_tag
    store.form.image = 'xxx.io/xxa/vllm-ascend:0.313'
    await store.submitCreate()
    spec = vi.mocked(createDeploy).mock.calls[1][0].spec
    expect(spec?.image).toBe('xxx.io/xxa/vllm-ascend:0.313')
    expect(spec?.image_tag).toBeUndefined()
  })

  it('镜像地址随预检下发(image 优先),并进基础预检缓存键', async () => {
    seed()
    store.form.image = 'xxx.io/xxa/vllm-ascend:0.313'
    vi.mocked(inspectDeployment).mockResolvedValue(resolvedWith())
    await store.runFinalInspect()
    const payload = vi.mocked(inspectDeployment).mock.calls[0][0]
    expect(payload.image).toBe('xxx.io/xxa/vllm-ascend:0.313')
    expect(payload.image_tag).toBeUndefined()
  })
})
