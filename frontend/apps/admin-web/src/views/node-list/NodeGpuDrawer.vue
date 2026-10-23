<script setup lang="ts">
import { NTag } from 'naive-ui'
import { useStore, gpuStatusMap, gpuSummary, type ComputeNode, type GpuData } from './useStore'

const props = defineProps<{
  show: boolean
  node: ComputeNode | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const { listNodeGpus } = useStore()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const gpus = ref<GpuData[]>([])
const loading = ref(false)

watch(
  () => props.show,
  async (visible) => {
    if (!visible || !props.node) return
    loading.value = true
    gpus.value = await listNodeGpus(props.node.id)
    loading.value = false
  },
)

/** 驱动信息汇总(取首卡;无 GPU 时为 —) */
const summary = computed(() => gpuSummary(gpus.value))
/** 驱动状态:首卡在线视为运行中 */
const driverRunning = computed(() => gpus.value[0]?.status === 'online')
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="680"
  >
    <n-drawer-content
      :title="`${node?.hostname ?? ''} · GPU管理`"
      closable
    >
      <n-spin :show="loading">
        <div
          v-if="gpus.length"
          class="flex flex-col gap-[1rem]"
        >
          <!-- 驱动信息 -->
          <section>
            <div class="drawer-subtitle">
              驱动信息
            </div>
            <n-descriptions
              size="small"
              :column="1"
              bordered
              label-placement="left"
              label-class="w-[16rem]"
            >
              <n-descriptions-item label="驱动版本">
                {{ summary.driver }}
              </n-descriptions-item>
              <n-descriptions-item label="驱动状态">
                <span :class="driverRunning ? 'text-success' : 'text-error'">
                  {{ driverRunning ? '运行中' : '异常' }}
                </span>
              </n-descriptions-item>
              <n-descriptions-item label="GPU数量">
                {{ summary.label }}
              </n-descriptions-item>
            </n-descriptions>
          </section>

          <!-- GPU 列表 -->
          <section>
            <div class="drawer-subtitle">
              GPU列表
            </div>
            <div class="flex flex-col border px-[1.2rem]">
              <div
                v-for="g in gpus"
                :key="g.id"
                class="py-[2.4rem] flex flex-col gap-[.375rem] border-b last:border-b-0"
              >
                <div class="flex items-center gap-[1rem]">
                  <span class="text-base font-bold">GPU {{ g.index }}</span>
                  <n-tag
                    size="small"
                    :type="(gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).type"
                    :bordered="false"
                  >
                    {{ (gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).label }}
                  </n-tag>
                </div>
                <n-descriptions
                  size="small"
                  :column="1"
                  label-placement="left"
                  label-class="w-[12rem]"
                >
                  <n-descriptions-item label="型号">
                    {{ g.name }}
                  </n-descriptions-item>
                  <n-descriptions-item label="架构">
                    {{ g.arch }}
                  </n-descriptions-item>
                  <n-descriptions-item label="显存">
                    {{ g.total_vram_gb }}GB
                  </n-descriptions-item>
                  <n-descriptions-item label="驱动版本">
                    {{ g.driver_version ?? '未安装' }}
                  </n-descriptions-item>
                  <n-descriptions-item label="UUID">
                    {{ g.uuid }}
                  </n-descriptions-item>
                </n-descriptions>
              </div>
            </div>
          </section>
        </div>
        <n-empty
          v-else-if="!loading"
          description="该节点暂无 GPU"
          class="py-[8rem]"
        />
        <div
          v-else
          class="min-h-[320px]"
        />
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped lang="scss">
.drawer-subtitle {
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
