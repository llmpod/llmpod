<script setup lang="ts">
import { AddOutline, ServerOutline, TimeOutline } from '@vicons/ionicons5'
import { message, copyToClipboard } from '@repo/hooks'
import { buildRegisterCommand, createBootstrapToken } from '@/api/node'
import { isSetupMock } from '@/api/quick-setup'
import { formatDateTime, nodeStatusMap } from '@/views/node-list/useStore'
import { useStore } from '../useStore'
import ClusterCreate from '../ClusterCreate.vue'

const { cluster, clusters, nodes, onlineNodes, switchCluster } = useStore()

/** 是否展示新建集群表单(默认直接注册节点,点击「新建集群」才切换) */
const showClusterForm = ref(false)

/** 集群下拉选项(存在多个集群时供用户切换注册目标) */
const clusterOptions = computed(() =>
  clusters.value.map((item) => ({ label: item.name, value: item.id })),
)

/** 下拉选择回调:n-select 的值可能为 string|number|null,仅接受 number 集群 id */
function onClusterSelect(value: string | number | null): void {
  if (typeof value === 'number') void switchCluster(value)
}

/**
 * 注册命令:与节点列表「注册节点」一致,调 /admin/nodes/bootstrap-token 以表单默认配置
 * (只传 cluster_id,ttl/容器名/数据卷等留空走后端缺省)签发,展示后端拼好的完整命令;
 * mock 模式(无后端)保留演示命令兜底
 */
const command = ref('')
/** 签发中/失败状态(失败给重试入口,不阻塞下方节点列表展示) */
const commandLoading = ref(false)
const commandError = ref(false)
/** 令牌过期时间(展示提示用;mock 无) */
const expiresAt = ref<string | null>(null)
/** 竞态守卫:慢响应回来前集群已切换则丢弃 */
let issueSeq = 0

async function issueCommand(): Promise<void> {
  const target = cluster.value
  if (!target) return
  // mock 模式无后端,本地拼演示命令
  if (isSetupMock()) {
    command.value = buildRegisterCommand(target.name)
    expiresAt.value = null
    return
  }
  const seq = ++issueSeq
  commandLoading.value = true
  commandError.value = false
  try {
    // 默认配置:仅 cluster_id,其余字段留空不传(与 NodeRegisterModal 默认表单一致)
    const data = await createBootstrapToken({ cluster_id: target.id })
    if (seq !== issueSeq) return
    command.value = data.command ?? ''
    expiresAt.value = data.expires_at
  }
  catch {
    if (seq !== issueSeq) return
    command.value = ''
    expiresAt.value = null
    commandError.value = true
  }
  finally {
    if (seq === issueSeq) commandLoading.value = false
  }
}

/** 集群就绪/切换/新建后重新签发(令牌面向当前集群,旧命令随之失效) */
watch(
  () => cluster.value?.id,
  () => void issueCommand(),
  { immediate: true },
)

/** 复制成功后短暂显示对勾 */
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined

/** 复制注册命令 */
async function copyCommand() {
  if (!command.value) return
  try {
    await copyToClipboard(command.value)
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1500)
  }
  catch {
    message.error('复制失败,请手动复制')
  }
}

/** 下一步按钮禁用态:无在线节点或正在新建集群时不可点 */
const canNext = computed(() => onlineNodes.value.length > 0 && !showClusterForm.value)

/** 「下一步」禁用提示:与下方 validate 提示文案一致(新建集群表单展开时优先提示) */
const nextDisabledTip = computed(() => {
  if (showClusterForm.value) return '请先完成或取消新建集群'
  if (!onlineNodes.value.length) {
    return nodes.value.length > 0
      ? '节点暂不在线,请等待至少一个节点上线'
      : '请先在节点上执行注册命令'
  }
  return undefined
})

/** 下一步门控:至少 1 个在线节点 */
function validate(): Promise<boolean> {
  if (onlineNodes.value.length > 0) {
    if (nodes.value.some((node) => node.status !== 'online')) {
      message.info('部分节点暂不在线,可稍后在选择节点时使用')
    }
    return Promise.resolve(true)
  }
  message.warning(
    nodes.value.length > 0 ? '节点暂不在线,请等待至少一个节点上线' : '请先在节点上执行注册命令',
  )
  return Promise.resolve(false)
}

defineExpose({ canNext, nextDisabledTip, validate })
</script>

<template>
  <div class="flex flex-col gap-[1.6rem]">
    <!-- 当前集群卡片:单集群开箱即用,多集群时下拉切换;「新建集群」点击后原位置展开创建表单 -->
    <div class="rounded-[1.2rem] border border-line bg-fill-1 px-[2rem] py-[1.6rem]">
      <n-collapse-transition :show="!showClusterForm">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-[.8rem] min-w-0">
            <n-icon
              :component="ServerOutline"
              :size="20"
              class="text-primary shrink-0"
            />
            <!-- 多集群:下拉切换注册目标,类型/描述/命令/节点列表随选中联动;单集群:直接展示名称 -->
            <template v-if="clusters.length > 1">
              <span class="text-strong font-semibold shrink-0">集群</span>
              <LmSelect
                :value="cluster?.id ?? null"
                :options="clusterOptions"
                size="small"
                class="w-[16rem] shrink-0"
                @update:value="onClusterSelect"
              />
            </template>
            <span
              v-else
              class="text-strong font-semibold truncate"
            >{{ cluster?.name }}</span>
            <n-tag
              size="small"
              :bordered="false"
              type="info"
              class="shrink-0"
            >
              {{ cluster?.type === 'k8s' ? 'Kubernetes' : 'Docker' }}
            </n-tag>
            <span
              v-if="cluster?.description"
              class="text-small text-secondary truncate"
            >
              {{ cluster.description }}
            </span>
          </div>
          <n-button
            size="small"
            ghost
            type="default"
            @click="showClusterForm = true"
          >
            <template #icon>
              <n-icon :component="AddOutline" />
            </template>
            新建集群
          </n-button>
        </div>
      </n-collapse-transition>
      <n-collapse-transition :show="showClusterForm">
        <ClusterCreate
          @created="showClusterForm = false"
          @cancel="showClusterForm = false"
        />
      </n-collapse-transition>
    </div>

    <template v-if="!showClusterForm">
      <!-- 注册命令:在目标节点上执行即可加入当前集群 -->
      <div class="rounded-[1.2rem] border border-line bg-fill-1 p-[2rem]">
        <div class="text-strong font-semibold">
          注册节点
        </div>
        <p class="text-small text-secondary my-[.8rem]">
          在要加入集群的节点上执行以下命令,注册成功后自动出现在下方列表:
        </p>
        <div class="cmd">
          <pre
            v-if="command"
            class="cmd__text"
          >{{ command }}</pre>
          <!-- 签发中/集群未就绪:占位文案,避免空白面板 -->
          <div
            v-else-if="commandLoading || !commandError"
            class="cmd__text cmd__text--muted"
          >
            正在生成注册命令…
          </div>
          <!-- 签发失败:占位 + 重试入口(全局中间件已提示错误原因) -->
          <template v-else>
            <div class="cmd__text cmd__text--muted">
              注册命令生成失败
            </div>
            <n-button
              size="tiny"
              quaternary
              type="primary"
              @click="issueCommand"
            >
              重试
            </n-button>
          </template>
          <n-button
            v-if="command"
            class="cmd__copy"
            :class="{ 'cmd__copy--copied': copied }"
            text
            title="复制命令"
            @click="copyCommand"
          >
            <svg
              v-if="copied"
              class="w-[1.6rem]"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M5 13l4 4L19 7" />
            </svg>
            <svg
              v-else
              class="w-[1.6rem]"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <rect
                x="8"
                y="8"
                width="12"
                height="12"
                rx="2"
              />
              <path d="M16 8V6a2 2 0 0 0 -2 -2H6a2 2 0 0 0 -2 2v8a2 2 0 0 0 2 2h2" />
            </svg>
          </n-button>
        </div>
        <!-- 令牌有效期提示(后端签发返回;有效期内同一命令可引导多台机器) -->
        <div
          v-if="command && expiresAt"
          class="mt-[.8rem] flex items-center gap-[.4rem] text-base text-secondary"
        >
          <n-icon
            :component="TimeOutline"
            :size="14"
          />
          令牌有效至 {{ formatDateTime(expiresAt) }}(本地时间),有效期内可重复引导多台机器
        </div>
      </div>

      <!-- 已注册节点(轮询自动刷新) -->
      <div class="rounded-[1.2rem] border border-line bg-fill-1">
        <div class="px-[1.6rem] py-[1rem] text-strong font-semibold border-b border-line">
          已注册节点
        </div>
        <div
          v-if="!nodes.length"
          class="px-[1.6rem] py-[1.6rem] text-small text-secondary"
        >
          暂无已注册节点,等待节点执行注册命令…
        </div>
        <div
          v-for="node in nodes"
          :key="node.id"
          class="flex items-center gap-[1.2rem] px-[1.6rem] py-[1rem] border-b border-line last:border-b-0"
        >
          <span class="text-default">{{ node.hostname }}（{{ node.ip }}）</span>
          <n-tag
            size="small"
            :bordered="false"
            :type="(nodeStatusMap[node.status] ?? { label: node.status, type: 'default' }).type"
          >
            {{ (nodeStatusMap[node.status] ?? { label: node.status, type: 'default' }).label }}
          </n-tag>
          <span
            v-if="node.hardware"
            class="text-small text-secondary"
          >{{ node.hardware }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped lang="scss">
/* 注册命令终端风格面板(与 NodeRegisterModal 一致) */
.cmd {
  position: relative;
  background-color: #1b1b1f;
  border-radius: 6px;
  padding: 12px 14px;

  &__text {
    font-family: monospace;
    font-size: 12px;
    line-height: 1.8;
    color: #d4d4d8;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
  }

  /* 签发中/失败占位文案(与 NodeRegisterModal 的 __note 同色) */
  &__text--muted {
    color: #7c7c85;
  }

  &__copy {
    position: absolute;
    top: 8px;
    right: 8px;
    display: flex;
    padding: 4px;
    border-radius: 4px;
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
