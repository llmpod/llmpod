<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import {
  useStore,
  logStatusMap,
  formatDecimal,
  formatNumber,
  formatDateTime,
  type RequestLogDetailData,
} from './useStore'

const props = defineProps<{ requestId: string }>()

const { getDetail } = useStore()
/** shallowRef 避免大体积 request_body/response_body 被深层 Proxy 递归代理 */
const detail = shallowRef<RequestLogDetailData>()
const loading = ref(false)
const loadFailed = ref(false)

let abortController: AbortController | null = null

async function fetchDetail() {
  abortController?.abort()
  const controller = new AbortController()
  abortController = controller

  loading.value = true
  loadFailed.value = false
  try {
    const data = await getDetail(props.requestId, controller.signal)
    if (!controller.signal.aborted) {
      detail.value = data
    }
  } catch {
    if (!controller.signal.aborted) {
      loadFailed.value = true
    }
  } finally {
    if (!controller.signal.aborted) {
      loading.value = false
    }
  }
}

onMounted(() => {
  void fetchDetail()
})

onBeforeUnmount(() => {
  abortController?.abort()
  abortController = null
})
</script>

<template>
  <n-spin
    :show="loading"
    class="min-h-[12rem]"
  >
    <div
      v-if="loadFailed && !detail && !loading"
      class="flex flex-col items-center justify-center gap-[1rem] py-[3rem]"
    >
      <n-empty
        size="small"
        description="日志详情加载失败"
      />
      <n-button
        size="small"
        @click="fetchDetail"
      >
        重新加载
      </n-button>
    </div>
    <div
      v-else-if="detail"
      class="flex flex-col gap-[1rem]"
    >
      <!-- 基础信息 -->
      <section>
        <div class="text-small font-semibold text-strong mb-[.8rem]">
          基础信息
        </div>
        <n-descriptions
          size="small"
          :column="2"
          bordered
          label-placement="left"
        >
          <n-descriptions-item label="请求 ID">
            {{ detail.request_id }}
          </n-descriptions-item>
          <n-descriptions-item label="状态">
            <n-tag
              size="small"
              :type="(logStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).type"
              :bordered="false"
            >
              {{ (logStatusMap[detail.status] ?? { label: detail.status, type: 'default' }).label }}
            </n-tag>
          </n-descriptions-item>
          <n-descriptions-item label="模型别名">
            {{ detail.model_alias }}
          </n-descriptions-item>
          <n-descriptions-item label="操作">
            {{ detail.operation }}
          </n-descriptions-item>
          <n-descriptions-item label="协议">
            {{ detail.protocol ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="重试次数">
            {{ detail.retry_count }}
          </n-descriptions-item>
          <n-descriptions-item label="API Key ID">
            {{ detail.api_key_id }}
          </n-descriptions-item>
          <n-descriptions-item label="HTTP 状态码">
            {{ detail.http_status ?? '—' }}
          </n-descriptions-item>
        </n-descriptions>
      </section>

      <!-- 计量信息 -->
      <section>
        <div class="text-small font-semibold text-strong mb-[.8rem]">
          计量信息
        </div>
        <n-descriptions
          size="small"
          :column="2"
          bordered
          label-placement="left"
        >
          <n-descriptions-item label="输入 Token">
            {{ formatNumber(detail.prompt_tokens) }}
          </n-descriptions-item>
          <n-descriptions-item label="输出 Token">
            {{ formatNumber(detail.completion_tokens) }}
          </n-descriptions-item>
          <n-descriptions-item label="总 Token">
            {{ formatNumber(detail.total_tokens) }}
          </n-descriptions-item>
          <n-descriptions-item label="缓存 Token">
            {{ formatNumber(detail.cached_tokens) }}
          </n-descriptions-item>
          <n-descriptions-item label="费用">
            {{ formatDecimal(detail.cost) }}
          </n-descriptions-item>
          <n-descriptions-item label="延迟">
            {{ detail.latency_ms == null ? '—' : `${detail.latency_ms} ms` }}
          </n-descriptions-item>
          <n-descriptions-item label="请求时间">
            {{ formatDateTime(detail.created_at) }}
          </n-descriptions-item>
          <n-descriptions-item label="是否截断">
            {{ detail.truncated ? '是' : '否' }}
          </n-descriptions-item>
          <n-descriptions-item label="快照关闭">
            {{ detail.snapshot_disabled ? '是' : '否' }}
          </n-descriptions-item>
          <n-descriptions-item label="错误信息">
            {{ detail.error_message ?? '—' }}
          </n-descriptions-item>
        </n-descriptions>
      </section>

      <!-- 请求体 -->
      <section>
        <LmVirtualText
          title="请求体"
          :content="detail.request_body"
        />
      </section>

      <!-- 响应体 -->
      <section>
        <LmVirtualText
          title="响应体"
          :content="detail.response_body"
        />
      </section>
    </div>
  </n-spin>
</template>

<style scoped>
:deep(.n-descriptions.n-descriptions--bordered .n-descriptions-table-wrapper .n-descriptions-table .n-descriptions-table-row .n-descriptions-table-header){
  @apply whitespace-nowrap
}
:deep(.n-descriptions .n-descriptions-table-wrapper .n-descriptions-table .n-descriptions-table-row .n-descriptions-table-content){
  vertical-align: middle;
}
</style>
