<script setup lang="ts">
import { NButton, NIcon, NInput } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import {
  AddOutline,
  AlertCircleOutline,
  ArrowForwardOutline,
  CheckmarkOutline,
  ChevronDown,
  CopyOutline,
  InformationCircleOutline,
  TimeOutline,
  TrashOutline,
} from '@vicons/ionicons5'
import { FormCustom, FormInput, FormSelect, message, useDialog, useDialogClose, useForm, copyToClipboard } from '@repo/hooks'
import { LmSelect } from '@repo/naive-ui'
import { listClusters } from '@/api/cluster'
import {
  createBootstrapToken,
  listNodes,
  type BootstrapTokenData,
  type BootstrapTokenPayload,
  type NodeData,
} from '@/api/node'
import PoolForm from '../node-pool/PoolForm.vue'
import { formatDateTime, notify } from './useStore'

/** 弹窗内自关(useDialog 挂载,无底部按钮) */
const close = useDialogClose()

/* ---------------- 校验规则(与后端白名单对齐,非法值直接 422) ---------------- */

/** IPv4(逐段 0-255)或宽松 IPv6(仅 hex 与冒号,至少一个冒号);空=自动探测 */
function isValidIp(value: string): boolean {
  if (/^(\d{1,3})(\.\d{1,3}){3}$/.test(value)) {
    return value.split('.').every((seg) => Number(seg) <= 255)
  }
  return value.includes(':') && /^[0-9a-fA-F:]+$/.test(value)
}

/** docker 具名卷(同容器名规则)或宿主绝对路径(路径段仅 [A-Za-z0-9._-],拒 .. 段与裸 /) */
function isValidVolumeOrPath(value: string): boolean {
  if (/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(value)) return true
  if (!value.startsWith('/') || value === '/') return false
  if (!/^[A-Za-z0-9._/-]+$/.test(value)) return false
  return value.split('/').slice(1).every((seg) => seg !== '' && seg !== '..')
}

/** 绝对路径(容器内目录/宿主侧目录通用;段仅 [A-Za-z0-9._-],拒 .. 段,允许可选尾斜杠) */
function isValidAbsPath(value: string): boolean {
  if (!/^\/[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)*\/?$/.test(value)) return false
  return value
    .replace(/\/+$/, '')
    .split('/')
    .slice(1)
    .every((seg) => seg !== '..')
}

/** 镜像 ref 全形态(registry/repo:tag@digest,不含 +) */
function isValidImageRef(value: string): boolean {
  return /^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,255}$/.test(value)
}

const ipRule = {
  validator: (_rule: unknown, value: string) => !value || isValidIp(value),
  message: '须为合法 IPv4/IPv6 地址',
  trigger: 'blur' as const,
}
const containerNameRule = {
  validator: (_rule: unknown, value: string) =>
    !value || /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(value),
  message: '须以字母/数字开头,仅含字母/数字/._-,最长 64 字符',
  trigger: 'blur' as const,
}
const volumeRule = {
  validator: (_rule: unknown, value: string) => !value || isValidVolumeOrPath(value),
  message: '须为具名卷或宿主绝对路径(路径段不含 ..)',
  trigger: 'blur' as const,
}
const absPathRule = {
  validator: (_rule: unknown, value: string) => !value || isValidAbsPath(value),
  message: '须为绝对路径,且路径段不含 ..',
  trigger: 'blur' as const,
}
const imageRule = {
  validator: (_rule: unknown, value: string) => !value || isValidImageRef(value),
  message: '镜像 ref 不合法(仅字母/数字/._/:@-,须以字母/数字开头)',
  trigger: 'blur' as const,
}

/* ---------------- 表单 ---------------- */

/** 令牌有效期档位(秒);28800=8h 为后端默认,选默认值时不上传该参数(命令更干净) */
const TTL_OPTIONS: SelectOption[] = [
  { label: '1 小时', value: 3600 },
  { label: '4 小时', value: 14400 },
  { label: '8 小时(默认)', value: 28800 },
  { label: '24 小时', value: 86400 },
]
const TTL_DEFAULT = 28800

/** 额外挂载上限(后端限制 16 条) */
const MAX_MOUNTS = 16

/** 额外挂载行:src 宿主目录/具名卷 + dst 容器内绝对路径 + mode(ro/rw/z,空=不指定) */
interface MountRow {
  src: string
  dst: string
  mode: string
}

interface RegisterFormValues {
  cluster_id: number | null
  ttl: number
  node_ip: string
  container_name: string
  data_volume: string
  image: string
  model_host_dir: string
  mounts: MountRow[]
}

/** 启用中集群下拉选项(inactive 集群签发会 404,只拉 active) */
const clusterOptions = ref<SelectOption[]>([])
const clustersLoading = ref(false)

/** 拉取启用中集群选项 */
async function refreshClusters(): Promise<void> {
  clustersLoading.value = true
  try {
    const page = await listClusters({ status: 'active', page: 1, page_size: 100 })
    clusterOptions.value = page.items.map((cluster) => ({
      label: `${cluster.name}(${cluster.node_count} 节点 / ${cluster.gpu_count} GPU)`,
      value: cluster.id,
    }))
  } finally {
    clustersLoading.value = false
  }
}

/** 挂载(即弹窗打开)时拉取启用中集群,默认选中第一个 */
onMounted(async () => {
  await refreshClusters()
  if (clusterOptions.value.length > 0) {
    setValues({ cluster_id: clusterOptions.value[0].value as number })
  }
})

/**
 * 集群下拉空态入口:打开创建集群弹窗(复用节点池 PoolForm,内部自治校验/保存/关闭);
 * 关闭后刷新下拉,对比前后选项差异自动选中新建的集群(取消/未创建则保持不动)
 */
function openClusterCreate() {
  const beforeIds = new Set(clusterOptions.value.map((option) => option.value as number))
  useDialog({
    title: '创建集群',
    component: PoolForm,
    area: 50,
    btn: { confirmText: '创建', cancelText: '取消' },
    dialogProps: { maskClosable: false },
    onClose: () => {
      void refreshClusters().then(() => {
        const created = clusterOptions.value.find(
          (option) => !beforeIds.has(option.value as number),
        )
        if (created) setValues({ cluster_id: created.value as number })
      })
    },
  })
}

/** 高级配置默认收起(95% 场景选完集群直接生成) */
const showAdvanced = ref(false)

/** 表单底部「高级配置/收起」入口(点击切换高级配置显隐);热区仅图标+文字,不占整行 */
const renderAdvancedToggle = () =>
  h('div', { class: 'my-[.6rem]' }, [
    h(
      'span',
      {
        class:
          'inline-flex cursor-pointer select-none items-center gap-[.4rem] text-base text-secondary transition-colors hover:text-primary',
        onClick: () => {
          showAdvanced.value = !showAdvanced.value
        },
      },
      [
        // 收起时箭头朝右(点击展开),展开后朝下(点击收起)
        h(
          NIcon,
          {
            class: showAdvanced.value
              ? 'transition-transform'
              : 'transition-transform -rotate-90',
          },
          { default: () => h(ChevronDown) },
        ),
        showAdvanced.value ? '收起' : '高级配置',
      ],
    ),
  ])

/** 挂载模式下拉(ro 只读 / rw 读写 / z SELinux 共享标签),留空=不指定 mode */
const MOUNT_MODE_OPTIONS = [
  { label: 'ro 只读', value: 'ro' },
  { label: 'rw 读写', value: 'rw' },
  { label: 'z SELinux', value: 'z' },
]

/**
 * 额外挂载动态行:宿主路径 →(箭头指示挂载方向) 容器路径 + mode 下拉,最多 MAX_MOUNTS 行;
 * grid 定列保证多行纵向对齐;删除草稿行走中性图标钮,红色只留给破坏性确认
 */
const renderMounts = (ctx: { model: RegisterFormValues }) => {
  const rows = ctx.model.mounts
  const addDisabled = rows.length >= MAX_MOUNTS
  return h('div', { class: 'flex w-full flex-col gap-[.8rem]' }, [
    h('div', { class: 'text-base text-secondary' }, [
      '将宿主目录或具名卷挂载进容器,如 ',
      h('span', { class: 'font-mono' }, '/usr/local/dcmi:/usr/local/dcmi:ro'),
    ]),
    ...rows.map((row, index) =>
      h(
        'div',
        {
          key: index,
          class: 'grid items-center gap-[.6rem]',
          style: { gridTemplateColumns: 'minmax(0, 1fr) auto minmax(0, 1fr) 8rem auto' },
        },
        [
          h(NInput, {
            value: row.src,
            size: 'small',
            placeholder: '宿主路径 / 具名卷',
            'onUpdate:value': (value: string) => {
              row.src = value
            },
          }),
          h(
            NIcon,
            { size: 14, class: 'text-secondary' },
            { default: () => h(ArrowForwardOutline) },
          ),
          h(NInput, {
            value: row.dst,
            size: 'small',
            placeholder: '容器内路径',
            'onUpdate:value': (value: string) => {
              row.dst = value
            },
          }),
          h(LmSelect, {
            value: row.mode || null,
            size: 'small',
            options: MOUNT_MODE_OPTIONS,
            placeholder: '默认',
            'onUpdate:value': (value: string | null) => {
              row.mode = value ?? ''
            },
          }),
          h(
            NButton,
            {
              size: 'small',
              quaternary: true,
              title: '删除本行',
              onClick: () => {
                rows.splice(index, 1)
              },
            },
            { icon: () => h(NIcon, null, { default: () => h(TrashOutline) }) },
          ),
        ],
      ),
    ),
    h(
      NButton,
      {
        size: 'small',
        dashed: true,
        disabled: addDisabled,
        onClick: () => {
          rows.push({ src: '', dst: '', mode: '' })
        },
      },
      {
        icon: () => h(NIcon, null, { default: () => h(AddOutline) }),
        default: () => (addDisabled ? `最多 ${MAX_MOUNTS} 条` : '添加挂载'),
      },
    ),
  ])
}

/** 额外挂载整段校验:空行放行(提交时过滤),填了的行 src/dst 均须合法,且不可重复 */
const mountsRule = {
  validator: (_rule: unknown, rows: MountRow[]) => {
    const specs: string[] = []
    for (let i = 0; i < rows.length; i++) {
      const src = rows[i].src.trim()
      const dst = rows[i].dst.trim()
      if (!src && !dst) continue
      if (!src) return new Error(`第 ${i + 1} 行:挂载源不能为空`)
      if (!isValidVolumeOrPath(src)) return new Error(`第 ${i + 1} 行:挂载源须为具名卷或宿主绝对路径`)
      if (!dst) return new Error(`第 ${i + 1} 行:容器路径不能为空`)
      if (!isValidAbsPath(dst)) return new Error(`第 ${i + 1} 行:容器路径须为绝对路径`)
      specs.push(rows[i].mode ? `${src}:${dst}:${rows[i].mode}` : `${src}:${dst}`)
    }
    if (new Set(specs).size !== specs.length) return new Error('存在重复的挂载项')
    return true
  },
  trigger: ['change', 'blur'],
}

/** 只传用户明确修改过的字段(空值不上传,保持引导命令 URL 干净) */
function buildPayload(model: RegisterFormValues): BootstrapTokenPayload {
  const mounts = model.mounts
    .filter((row) => row.src.trim() && row.dst.trim())
    .map((row) =>
      row.mode ? `${row.src.trim()}:${row.dst.trim()}:${row.mode}` : `${row.src.trim()}:${row.dst.trim()}`,
    )
  return {
    cluster_id: model.cluster_id!,
    ...(model.ttl !== TTL_DEFAULT && { ttl: model.ttl }),
    ...(model.node_ip.trim() && { node_ip: model.node_ip.trim() }),
    ...(model.container_name.trim() && { container_name: model.container_name.trim() }),
    ...(model.data_volume.trim() && { data_volume: model.data_volume.trim() }),
    ...(model.image.trim() && { image: model.image.trim() }),
    ...(model.model_host_dir.trim() && { model_host_dir: model.model_host_dir.trim() }),
    ...(mounts.length > 0 && { extra_mounts: mounts }),
  }
}

const { LmForm, submit, submitting, setValues } = useForm<RegisterFormValues>({
  data: () => ({
    cluster_id: null as number | null,
    ttl: TTL_DEFAULT,
    node_ip: '',
    container_name: '',
    data_volume: '',
    image: '',
    model_host_dir: '',
    mounts: [] as MountRow[],
  }),
  options: () => [
    FormSelect('所属集群', 'cluster_id', clusterOptions.value, {
      rules: [
        { required: true, type: 'number', message: '请选择所属集群', trigger: ['blur', 'change'] },
      ],
      attrs: { placeholder: '请选择集群', loading: clustersLoading.value },
      // 无可用集群时在下拉选项区给创建入口,点击直接打开创建集群弹窗
      controlSlots: {
        empty: () =>
          h(
            'div',
            {
              class:
                'flex cursor-pointer items-center justify-center gap-[.4rem] py-[1rem] text-small text-secondary transition-colors hover:text-primary',
              onClick: openClusterCreate,
            },
            [h(NIcon, { size: 14 }, { default: () => h(AddOutline) }), '暂无可用集群,点击创建'],
          ),
      },
    }),
    FormSelect('令牌有效期', 'ttl', TTL_OPTIONS, { attrs: { clearable: false } }),
    FormCustom(renderAdvancedToggle),
    // ---- 高级配置:默认收起;placeholder 只放默认值/留空语义,详细说明见对接文档 ----
    FormInput('节点互联 IP', 'node_ip', {
      visible: showAdvanced,
      rules: [ipRule],
      attrs: { placeholder: '留空自动探测(bridge 网络须填)' },
    }),
    FormInput('容器名', 'container_name', {
      visible: showAdvanced,
      rules: [containerNameRule],
      attrs: { placeholder: '默认 llmpod-node' },
    }),
    FormInput('数据卷', 'data_volume', {
      visible: showAdvanced,
      rules: [volumeRule],
      attrs: { placeholder: '默认 llmpod-node-data' },
    }),
    FormInput('镜像', 'image', {
      visible: showAdvanced,
      rules: [imageRule],
      attrs: { placeholder: '默认 llmpod:latest' },
    }),
    FormInput('模型目录(宿主)', 'model_host_dir', {
      visible: showAdvanced,
      rules: [absPathRule],
      attrs: { placeholder: '留空 = 与 Manager 配置一致(/data/models)' },
    }),
    FormCustom(renderMounts, {
      label: '额外挂载',
      field: 'mounts',
      wrapFormItem: true,
      visible: showAdvanced,
      rules: mountsRule,
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败 throw → 保留表单;HTTP 错误由全局中间件提示,同样保留表单
    await validate()
    const payload = buildPayload(model)
    const data = await createBootstrapToken(payload)
    bootstrap.value = data
    // 用 expires_at - ttl 估算服务端签发时刻(规避客户端时钟偏差),留 1s 容差
    const issuedAtMs = data.expires_at
      ? new Date(data.expires_at).getTime() - (payload.ttl ?? TTL_DEFAULT) * 1000 - 1000
      : Date.now()
    phase.value = 'result'
    startPolling(model.cluster_id!, model.node_ip.trim(), issuedAtMs)
  },
})

/** 生成按钮:触发表单提交;失败(校验/HTTP)保留表单,不冒泡未处理 rejection */
function onGenerate() {
  void submit().catch(() => {})
}

/* ---------------- 结果阶段:命令展示 + 轮询等待注册 ---------------- */

/** 弹窗阶段:form 表单 → result 命令展示与轮询 */
const phase = ref<'form' | 'result'>('form')
/** 签发结果(command 为后端拼好的完整命令,前端不自行拼接) */
const bootstrap = ref<BootstrapTokenData | null>(null)

/** 轮询状态:waiting 等待中 / registered 已登记未上线 / online 注册成功 / timeout 超时 */
type PollStatus = 'waiting' | 'registered' | 'online' | 'timeout'
const pollStatus = ref<PollStatus>('waiting')
/** 轮询命中的新节点(online/registered 时有值) */
const matchedNode = ref<NodeData | null>(null)

/** 步骤②副标题:随轮询状态给出当前进展 */
const pollStatusDesc = computed(() => {
  switch (pollStatus.value) {
    case 'waiting':
      return '每 3 秒自动检测,通常需要几秒到几分钟'
    case 'registered':
      return '节点已登记,等待 WebSocket 连接上线…'
    case 'online':
      return '已注册上线'
    case 'timeout':
      return '已等待 2 分钟'
  }
})

const POLL_INTERVAL_MS = 3000
const POLL_TIMEOUT_MS = 120_000
let pollTimer: ReturnType<typeof setInterval> | undefined

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

/**
 * 轮询集群节点列表确认注册(每 3s,约 2 分钟):
 * 填了 node_ip 按 ip 匹配,否则取签发时刻之后新增的记录;
 * offline 只是中间态(注册落库与心跳先后),以最终 online 为准,未 online 前继续轮询。
 */
function startPolling(clusterId: number, nodeIp: string, issuedAtMs: number) {
  stopPolling()
  pollStatus.value = 'waiting'
  matchedNode.value = null
  const deadline = Date.now() + POLL_TIMEOUT_MS
  pollTimer = setInterval(async () => {
    if (Date.now() > deadline) {
      pollStatus.value = 'timeout'
      stopPolling()
      return
    }
    try {
      const page = await listNodes({ cluster_id: clusterId, page: 1, page_size: 20 })
      const hit = nodeIp
        ? page.items.find((node) => node.ip === nodeIp)
        : page.items.find((node) => new Date(node.created_at).getTime() > issuedAtMs)
      if (!hit) return
      matchedNode.value = hit
      if (hit.status === 'online') {
        pollStatus.value = 'online'
        stopPolling()
        notify()
        message.success(`节点已注册上线:${hit.hostname}`)
      } else {
        pollStatus.value = 'registered'
      }
    } catch {
      // 单次轮询失败(网络抖动等)静默重试,不打断等待
    }
  }, POLL_INTERVAL_MS)
}

/** 重新生成:回到表单(表单值保留;原令牌过期前仍有效,重复签发互不影响) */
function regenerate() {
  stopPolling()
  bootstrap.value = null
  matchedNode.value = null
  pollStatus.value = 'waiting'
  phase.value = 'form'
}

/** 关闭弹窗;已进入结果阶段说明可能已注册/签发,通知列表刷新 */
function finish() {
  stopPolling()
  if (phase.value === 'result') notify()
  close?.()
}

/** 复制成功后短暂显示对勾 */
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined

/** 复制引导命令(须整行复制,截断会导致令牌校验失败) */
async function copyCommand() {
  if (!bootstrap.value?.command) return
  try {
    await copyToClipboard(bootstrap.value.command)
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    message.error('复制失败,请手动复制')
  }
}

onBeforeUnmount(() => {
  stopPolling()
  clearTimeout(copyTimer)
})
</script>

<template>
  <!-- 表单阶段:选集群(+高级配置) → 签发引导令牌 -->
  <div v-if="phase === 'form'" class="flex flex-col">
    <LmForm />
    <div class="flex justify-end gap-[.8rem] pt-[1.6rem]">
      <n-button @click="close?.()"> 取消 </n-button>
      <n-button
        type="primary"
        :loading="submitting"
        :disabled="!clusterOptions.length"
        @click="onGenerate"
      >
        生成引导命令
      </n-button>
    </div>
  </div>

  <!-- 结果阶段:纵向时间线,终端块是唯一视觉锚点,凭据警示降级为终端注释行 -->
  <div v-else class="flex flex-col gap-[1.6rem]">
    <ol class="m-0 flex list-none flex-col p-0">
      <!-- ① 复制并执行引导命令:空心圆点=待用户操作 -->
      <li class="relative pb-[2rem] pl-[3.4rem]">
        <span class="absolute bottom-[1rem] left-[1.05rem] top-[2.8rem] w-px bg-line-strong" />
        <span
          class="absolute left-0 top-0 flex h-[2.2rem] w-[2.2rem] items-center justify-center rounded-full border border-primary text-small font-semibold text-primary"
        >
          1
        </span>
        <div class="flex flex-col gap-[.2rem]">
          <span class="text-strong font-semibold">复制并执行引导命令</span>
          <span class="text-base text-secondary">在目标 GPU 机器的 shell 中整行执行</span>
        </div>
        <div class="cmd mt-[1rem]">
          <pre class="cmd__text">{{ bootstrap?.command }}</pre>
          <div class="cmd__note"># 令牌含集群凭据,请勿外传(截图/文档需打码)</div>
          <n-button
            class="cmd__copy"
            :class="{ 'cmd__copy--copied': copied }"
            text
            title="复制命令"
            @click="copyCommand"
          >
            <n-icon :component="copied ? CheckmarkOutline : CopyOutline" :size="16" />
          </n-button>
        </div>
        <div class="mt-[.8rem] flex items-center gap-[.4rem] text-base text-secondary">
          <n-icon :component="TimeOutline" :size="14" />
          令牌有效至 {{ formatDateTime(bootstrap?.expires_at ?? null) }}(本地时间),有效期内可重复引导多台机器
        </div>
      </li>

      <!-- ② 等待节点注册上线:圆点随状态变化(spinner/对勾/感叹) -->
      <li class="relative pl-[3.4rem]">
        <span
          class="absolute left-0 top-0 flex h-[2.2rem] w-[2.2rem] items-center justify-center rounded-full"
          :class="
            pollStatus === 'online'
              ? 'bg-success text-inverse'
              : pollStatus === 'timeout'
                ? 'bg-warning text-inverse'
                : 'border border-primary bg-primary-soft text-primary'
          "
        >
          <n-icon v-if="pollStatus === 'online'" :component="CheckmarkOutline" :size="14" />
          <n-icon v-else-if="pollStatus === 'timeout'" :component="AlertCircleOutline" :size="14" />
          <n-spin v-else :size="13" />
        </span>
        <div class="flex flex-col gap-[.2rem]">
          <span class="text-strong font-semibold">等待节点注册上线</span>
          <span class="text-base" :class="pollStatus === 'online' ? 'text-success' : 'text-secondary'">
            {{ pollStatusDesc }}
          </span>
        </div>

        <!-- 已登记未上线:轻量提示行(非阻塞,不占告警权重) -->
        <div
          v-if="pollStatus === 'registered'"
          class="mt-[1rem] flex items-start gap-[.5rem] text-small text-secondary"
        >
          <n-icon :component="InformationCircleOutline" :size="14" class="mt-[.3rem] shrink-0" />
          <span>容器已启动但 WebSocket 未连上 Manager,请检查目标机能否访问 Manager 服务端口。</span>
        </div>

        <!-- 注册成功:节点信息直接内联展示,不再套成功告警 -->
        <div
          v-if="pollStatus === 'online' && matchedNode"
          class="mt-[1rem] flex flex-wrap items-center gap-x-[1.6rem] gap-y-[.4rem]"
        >
          <span class="text-strong font-semibold">{{ matchedNode.hostname }}</span>
          <span class="font-mono text-small text-secondary">{{ matchedNode.ip }}</span>
          <n-tag size="small" type="success" :bordered="false"> 在线 </n-tag>
          <span class="text-small text-secondary">GPU × {{ matchedNode.runtime?.gpu_count ?? '—' }}</span>
          <span v-if="matchedNode.cpu_arch" class="text-small text-secondary">
            {{ matchedNode.cpu_arch }}
          </span>
        </div>

        <!-- 超时:唯一的告警态,排障指引前置 -->
        <n-alert
          v-if="pollStatus === 'timeout'"
          class="mt-[1rem]"
          type="warning"
          :bordered="false"
          title="超时未检测到新节点"
        >
          引导脚本可能没有执行成功,请到目标机排查:① 执行 docker logs llmpod-node 查看容器日志;
          ② 无外网环境先 docker save | docker load 预置镜像;③ 确认命令已整行复制并执行。
        </n-alert>
      </li>
    </ol>

    <div class="flex justify-end gap-[.8rem]">
      <n-button @click="regenerate"> 重新生成 </n-button>
      <n-button type="primary" @click="finish"> 稍后在节点列表查看 </n-button>
    </div>
  </div>
</template>

<style scoped lang="scss">
/* 引导命令终端风格面板(与 quick-setup StepNodes 一致) */
.cmd {
  position: relative;
  background-color: #1b1b1f;
  border-radius: 0.6rem;
  padding: 1.2rem 1.4rem;

  &__text {
    font-family: monospace;
    font-size: 1.2rem;
    line-height: 1.8;
    color: #d4d4d8;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
    padding-right: 2.4rem;
  }

  /* shell 注释行:承载凭据警示等轻提醒,替代弹窗内的告警条 */
  &__note {
    margin-top: 0.6rem;
    font-family: monospace;
    font-size: 1.2rem;
    line-height: 1.6;
    color: #7c7c85;
    white-space: pre-wrap;
    word-break: break-all;
    padding-right: 2.4rem;
  }

  &__copy {
    position: absolute;
    top: 0.8rem;
    right: 0.8rem;
    display: flex;
    padding: 0.4rem;
    border-radius: 0.4rem;
    color: #8f8f96;
    cursor: pointer;

    &:hover {
      color: var(--c-primary);
    }

    &--copied {
      color: var(--c-primary);
    }
  }
}
</style>
