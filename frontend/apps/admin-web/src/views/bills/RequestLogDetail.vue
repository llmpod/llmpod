<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  useStore,
  logStatusMap,
  upstreamTypeConf,
  formatDecimal,
  formatNumber,
  formatDateTime,
  type RequestLogDetailData,
} from './useStore'

const props = defineProps<{ requestId: string }>()

const { getDetail } = useStore()
const detail = ref<RequestLogDetailData>()
const loading = ref(false)

onMounted(async () => {
  loading.value = true
  try {
    detail.value = await getDetail(props.requestId)
  } finally {
    loading.value = false
  }
})

/** body 展示:JSON 格式化,非 JSON 原样;{ _raw } 取原串 */
function formatBody(body: unknown): string {
  if (body == null) return '—'
  if (typeof body === 'string') {
    try {
      return JSON.stringify(JSON.parse(body), null, 2)
    } catch {
      return body
    }
  }
  if (typeof body === 'object') {
    const raw = (body as { _raw?: unknown })._raw
    if (typeof raw === 'string') return raw
    try {
      return JSON.stringify(body, null, 2)
    } catch {
      return String(body)
    }
  }
  return String(body)
}
</script>

<template>
  <n-spin :show="loading">
    <div
      v-if="detail"
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
          <n-descriptions-item label="引擎模型">
            {{ detail.served_model ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="操作">
            {{ detail.operation }}
          </n-descriptions-item>
          <n-descriptions-item label="协议">
            {{ detail.protocol ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="引擎">
            {{ detail.engine ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="上游流向">
            <n-tag
              size="small"
              :type="upstreamTypeConf(detail.upstream_type).type"
              :bordered="false"
            >
              {{ upstreamTypeConf(detail.upstream_type).label }}
            </n-tag>
          </n-descriptions-item>
          <n-descriptions-item label="上游供应商">
            {{ detail.provider_name ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="端点 ID">
            {{ detail.endpoint_id ?? '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="重试次数">
            {{ detail.retry_count }}
          </n-descriptions-item>
          <n-descriptions-item label="API Key ID">
            {{ detail.api_key_id }}
          </n-descriptions-item>
          <n-descriptions-item label="租户 ID">
            {{ detail.tenant_id ?? '—' }}
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
          <n-descriptions-item label="错误详情">
            <span class="break-words">{{ detail.error_detail ?? '—' }}</span>
          </n-descriptions-item>
        </n-descriptions>
      </section>

      <!-- 请求体 -->
      <section>
        <div class="text-small font-semibold text-strong mb-[.8rem]">
          请求体
        </div>
        <pre class="m-[0] max-h-[24rem] overflow-auto rounded-md bg-fill-2 p-[1rem] text-xs leading-[1.6]">{{ formatBody(detail.request_body) }}</pre>
      </section>

      <!-- 响应体 -->
      <section>
        <div class="text-small font-semibold text-strong mb-[.8rem]">
          响应体
        </div>
        <pre class="m-[0] max-h-[24rem] overflow-auto rounded-md bg-fill-2 p-[1rem] text-xs leading-[1.6]">{{ formatBody(detail.response_body) }}</pre>
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
