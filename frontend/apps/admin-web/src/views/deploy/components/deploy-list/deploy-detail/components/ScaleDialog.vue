<script setup lang="ts">
import { GridOutline, HardwareChipOutline, LayersOutline, TrendingUpOutline } from '@vicons/ionicons5'
import { message, useDialogClose } from '@repo/hooks'
import { scaleDeployment, scalePreview, type ScalePreviewOut } from '@/api/deployment'

const props = defineProps<{
  deploymentId: string
  deploymentName: string
  /** 当前份数(拓扑快照;打开后以 preview 返回的权威值为准) */
  currentCopies: number
  /** 现存副本序号(升序,1 基人读序号;缩容 LIFO 移除预览用,缺层时按 1..N 推导) */
  replicaSeqs: number[]
  /** node_id → hostname 解析表(扩容落点预览展示可读名) */
  hostnameOf: Record<string, string>
  /** 提交成功后回调(详情页立即手动刷一轮,徽标转 SCALING) */
  onScaled?: () => void
}>()

const close = useDialogClose()

const preview = shallowRef<ScalePreviewOut | null>(null)
const previewLoading = ref(false)
/** preview 拉取失败(已切出重试态;HTTP 层已统一提示) */
const previewFailed = ref(false)
const submitting = ref(false)
/** 目标份数(null = 输入空值) */
const copies = ref<number | null>(null)

async function loadPreview() {
  previewLoading.value = true
  previewFailed.value = false
  try {
    const data = await scalePreview(props.deploymentId)
    preview.value = data
    copies.value = data.current_copies
  } catch {
    previewFailed.value = true
  } finally {
    previewLoading.value = false
  }
}
// 弹窗每次打开都全新挂载,顶层直接拉预览
void loadPreview()

/** 当前份数:preview 权威,未返回前用拓扑快照兜底 */
const current = computed(() => preview.value?.current_copies ?? props.currentCopies)
/** 单副本规格(卡数,创建后不可变) */
const perCopyGpus = computed(() => preview.value?.per_copy_gpus ?? 0)
/** 集群空闲卡可支撑的最大份数(含存量) */
const maxFeasible = computed(() => preview.value?.max_feasible_copies ?? current.value)
const freeGpus = computed(() => preview.value?.free_gpus ?? 0)
/** stepper 上限:扩不动时不压当前份数(保持可回退缩容) */
const stepperMax = computed(() => Math.max(maxFeasible.value, current.value))

/** 目标相对当前的方向;相同为 null */
const direction = computed<'up' | 'down' | null>(() => {
  if (copies.value == null) return null
  if (copies.value > current.value) return 'up'
  if (copies.value < current.value) return 'down'
  return null
})

/** 再扩一份还差的卡数(>0 表示空闲卡不足) */
const missingGpus = computed(() => {
  if (direction.value !== 'up' || !perCopyGpus.value) return 0
  return Math.max(0, perCopyGpus.value - freeGpus.value)
})

/** 缩容将移除的份(LIFO 最新序号先摘;replicas 层缺失按 1..N 推导) */
const removedSeqs = computed(() => {
  if (direction.value !== 'down') return []
  const seqs = props.replicaSeqs.length
    ? [...props.replicaSeqs]
    : Array.from({ length: current.value }, (_, index) => index + 1)
  return seqs.sort((a, b) => b - a).slice(0, current.value - (copies.value ?? current.value))
})

/** 扩容落点(suggested_placement 的节点集,hostname 可读化;差卡为空) */
const placementHostnames = computed(() => {
  const nodeIds = preview.value?.suggested_placement?.node_ids ?? []
  return nodeIds.map((id) => props.hostnameOf[id] ?? id)
})

/** 资源概览卡:标签/图标/值/单位(值由模板渲染,加载中走骨架占位) */
const statCards = computed(() => [
  { icon: LayersOutline, label: '当前份数', value: current.value, unit: '份' },
  {
    icon: HardwareChipOutline,
    label: '单副本规格',
    value: perCopyGpus.value || '--',
    unit: '张/份',
  },
  { icon: GridOutline, label: '集群空闲卡', value: freeGpus.value, unit: '张' },
  { icon: TrendingUpOutline, label: '可扩上限', value: maxFeasible.value, unit: '份' },
])

/** 确认可用:预览就绪 + 目标合法(≠当前;扩容须不超可扩上限) */
const canConfirm = computed(
  () =>
    !!preview.value &&
    !previewLoading.value &&
    !previewFailed.value &&
    !submitting.value &&
    direction.value !== null &&
    (direction.value !== 'up' || maxFeasible.value >= (copies.value ?? 0)),
)

/** 确认禁用原因(悬浮/旁注提示;预览失败态由重试行解释,不重复) */
const confirmReason = computed(() => {
  if (previewLoading.value) return '预览加载中'
  if (direction.value === null) return '目标份数与当前相同'
  if (direction.value === 'up' && maxFeasible.value < (copies.value ?? 0)) {
    return `空闲卡不足:再扩 1 份需 ${perCopyGpus.value} 张,当前空闲 ${freeGpus.value} 张`
  }
  return ''
})

async function onSubmit() {
  if (!canConfirm.value || copies.value == null) return
  submitting.value = true
  try {
    await scaleDeployment(props.deploymentId, copies.value)
    message.success(
      `已提交:${direction.value === 'up' ? '扩容' : '缩容'}至 ${copies.value} 份,进度随编排状态推进`,
    )
    props.onScaled?.()
    close()
  } catch {
    // 接口异常 HTTP 层已统一提示(409 防重入等),保留弹窗可重试
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-[1.2rem] pt-[.4rem]">
    <!-- 预览失败:HTTP 层已提示,这里给出重试入口(与方向说明同一视觉语言) -->
    <LmAlert
      v-if="previewFailed"
      type="error"
    >
      <div class="flex items-center justify-between gap-[1rem]">
        <span>扩缩预览加载失败</span>
        <n-button
          size="small"
          @click="loadPreview"
        >
          重试
        </n-button>
      </div>
    </LmAlert>

    <template v-else>
      <!-- 资源概览:份数/单副本规格/空闲卡/可扩上限(数字大、单位次级;加载中骨架占位) -->
      <div class="grid grid-cols-2 gap-[1rem]">
        <div
          v-for="card in statCards"
          :key="card.label"
          class="rounded-[.8rem] border border-line bg-fill-2 px-[1.2rem] py-[1rem]"
        >
          <div class="flex items-center justify-between">
            <span class="text-base text-secondary">{{ card.label }}</span>
            <n-icon
              class="text-icon"
              :component="card.icon"
              :size="16"
            />
          </div>
          <div class="mt-[.4rem]">
            <n-skeleton
              v-if="previewLoading"
              text
              width="4rem"
            />
            <span
              v-else
              class="text-xl font-semibold text-strong tabular-nums"
            >{{ card.value }} <span class="text-base font-normal text-secondary">{{ card.unit }}</span></span>
          </div>
        </div>
      </div>

      <div class="flex flex-col gap-[.6rem]">
        <div class="flex items-baseline justify-between gap-[1rem]">
          <span class="text-small font-medium text-default">目标份数</span>
          <span class="text-base text-tertiary">单副本规格不可变,仅调整份数</span>
        </div>
        <n-input-number
          v-model:value="copies"
          :min="1"
          :max="stepperMax"
          :precision="0"
          :disabled="previewLoading || submitting"
          class="w-full"
        />
      </div>

      <!-- 方向说明:扩容给落点/差卡提示;缩容列 LIFO 移除份与服务不断保证 -->
      <LmAlert
        v-if="direction === 'up'"
        type="primary"
      >
        <div class="flex flex-col gap-[.25rem]">
          <div>扩容至 {{ copies }} 份:<span class="font-medium">新增 {{ (copies ?? 0) - current }} 份 × {{ perCopyGpus }} 张</span></div>
          <div v-if="placementHostnames.length">
            建议落点:<span class="font-medium">{{ placementHostnames.join('、') }}</span>
          </div>
          <div
            v-else-if="missingGpus > 0"
            class="text-warning"
          >
            空闲卡不足(差 {{ missingGpus }} 张),无法再扩
          </div>
        </div>
      </LmAlert>
      <LmAlert
        v-else-if="direction === 'down'"
        type="warning"
      >
        <div class="flex flex-col gap-[.25rem]">
          <div>
            <span>缩容至 {{ copies }} 份:按 LIFO 移除 </span>
            <template v-if="removedSeqs.length">
              <template
                v-for="(seq, i) in removedSeqs"
                :key="seq"
              >
                <n-tag
                  size="small"
                  round
                >
                  {{ `副本${seq}` }}
                </n-tag>
                <span
                  v-if="i < removedSeqs.length - 1"
                  class="mx-[.1rem]"
                >、</span>
              </template>
            </template>
            <span v-else>--</span>
          </div>
          <div>移除流程:摘端点 → 排空请求 → 停容器,其余份服务不断</div>
        </div>
      </LmAlert>
      <div
        v-else
        class="text-small text-secondary"
      >
        调整目标份数后确认提交;扩缩期间状态徽标转为「扩缩容中」
      </div>
    </template>

    <!-- 底部操作(弹窗按钮自治:预览失败/目标非法时禁用确认) -->
    <div class="flex items-center justify-end gap-[.8rem]">
      <span
        v-if="confirmReason"
        class="text-small text-warning mr-auto"
      >{{ confirmReason }}</span>
      <n-button
        :disabled="submitting"
        @click="close"
      >
        取消
      </n-button>
      <n-button
        type="primary"
        :loading="submitting"
        :disabled="!canConfirm"
        @click="onSubmit"
      >
        确认调整
      </n-button>
    </div>
  </div>
</template>
