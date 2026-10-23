<script setup lang="ts">
import { message, copyToClipboard } from '@repo/hooks'
import { CheckmarkOutline, CopyOutline } from '@vicons/ionicons5'
import {
  brandIconOf,
  formatContextLength,
  formatDecimal,
  modelTypeMap,
  routeOriginMap,
  routeStatusMap,
  useStore,
  type RouteData,
} from './useStore'

const props = defineProps<{
  show: boolean
  model: RouteData | null
}>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

/** 数据面接入基址(与模型列表并行拉取;未加载或失败为 null) */
const { proxyBaseUrl } = useStore()

const innerShow = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
})

const router = useRouter()

/** API Base URL:按协议分行——OpenAI SDK 的 baseURL 需含版本根 /v1(SDK 只拼 /chat/completions),
 *  Anthropic SDK 的 baseURL 不含 /v1(SDK 自拼 /v1/messages),二者不同,分行展示各自可直贴 SDK 的地址 */
const apiBaseUrls = computed(() => {
  const base = proxyBaseUrl.value
  return [
    {
      key: 'baseUrlOpenai',
      label: 'OpenAI',
      value: base ? `${base}/v1` : '接入地址加载中…',
      // 协议品牌色(品牌固定色,不走主题 token)
      tagColor: { textColor: '#10A37F', borderColor: '#10A37F', color: 'transparent' },
    },
    {
      key: 'baseUrlAnthropic',
      label: 'Anthropic',
      value: base ?? '接入地址加载中…',
      tagColor: { textColor: '#D97757', borderColor: '#D97757', color: 'transparent' },
    },
  ]
})

/** 支持的接口(辅助信息:完整路径从域名根算起,与 SDK 实际请求一致) */
const apiEndpoints = [
  {
    key: 'openai',
    label: 'OpenAI',
    path: 'POST /v1/chat/completions',
    tagColor: { textColor: '#10A37F', borderColor: '#10A37F', color: 'transparent' },
  },
  {
    key: 'anthropic',
    label: 'Anthropic',
    path: 'POST /v1/messages',
    tagColor: { textColor: '#D97757', borderColor: '#D97757', color: 'transparent' },
  },
]

/** 复制成功后短暂显示对勾的标识 */
const copiedKey = ref('')
let copyTimer: ReturnType<typeof setTimeout> | undefined

/** 复制文本到剪贴板 */
async function copyText(text: string, key: string) {
  try {
    await copyToClipboard(text)
    copiedKey.value = key
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copiedKey.value = ''
    }, 1500)
  } catch {
    message.error('复制失败,请手动复制')
  }
}

// 卸载时清理复制回退定时器,避免回调在卸载后触发
onUnmounted(() => {
  if (copyTimer) clearTimeout(copyTimer)
})

/** 在线调试:关闭抽屉并跳转 Playground(仅已发布模型) */
function goPlayground() {
  if (!props.model) return
  innerShow.value = false
  router.push({ name: 'playground', query: { model: props.model.name } })
}

/** 获取 API Key:关闭抽屉并跳转 API Key 管理页 */
function goApiKey() {
  innerShow.value = false
  router.push({ name: 'api-key' })
}

/** 定价展示:Decimal 字符串去尾零;null=不计费 */
function priceText(value: string | null): string {
  return value == null ? '不计费' : `¥${formatDecimal(value)} / 百万 tokens`
}

/** 定价明细行 */
const billingRows = computed(() => {
  if (!props.model) return []
  return [
    {
      dimension: '输入(chat / embeddings / rerank)',
      price: priceText(props.model.input_price_per_mtok),
      desc: 'embeddings 与 rerank 按输入价全量计费',
    },
    {
      dimension: '输出(仅生成类)',
      price: priceText(props.model.output_price_per_mtok),
      desc: 'chat 补全输出 token',
    },
    {
      dimension: '缓存命中输入',
      price:
        props.model.cached_input_price_per_mtok == null
          ? '未配置,按输入全价计'
          : priceText(props.model.cached_input_price_per_mtok),
      desc: '命中前缀缓存的输入 token',
    },
  ]
})
</script>

<template>
  <n-drawer
    v-model:show="innerShow"
    :width="640"
  >
    <n-drawer-content closable>
      <template #header>
        <div
          v-if="model"
          class="flex w-full items-center gap-[.5rem]"
        >
          <div class="flex h-[2.4rem] w-[2.4rem] shrink-0 items-center justify-center rounded-md bg-fill-2">
            <n-icon
              :size="15"
              class="text-tertiary"
            >
              <component :is="brandIconOf(model.name)" />
            </n-icon>
          </div>
          <span>{{ model.name }}</span>
          <n-tag
            size="small"
            :type="(routeStatusMap[model.status] ?? { label: model.status, type: 'default' }).type"
            :bordered="false"
          >
            {{ (routeStatusMap[model.status] ?? { label: model.status, type: 'default' }).label }}
          </n-tag>
          <n-button
            v-if="model.status === 'published'"
            type="primary"
            size="small"
            class="ml-auto mr-[.5rem]"
            @click="goPlayground"
          >
            在线调试
          </n-button>
        </div>
      </template>

      <div
        v-if="model"
        class="flex flex-col gap-[1rem]"
      >
        <!-- 基本信息 -->
        <section>
          <div class="mb-[.5rem] text-sm font-semibold text-strong">
            基本信息
          </div>
          <n-descriptions
            size="small"
            :column="1"
            bordered
            label-placement="left"
            label-class="w-[10rem]"
          >
            <n-descriptions-item label="模型名称">
              <div class="flex items-center gap-[.25rem]">
                <span class="font-mono">{{ model.name }}</span>
                <n-button
                  text
                  size="tiny"
                  @click="copyText(model.name, 'modelName')"
                >
                  <n-icon :component="copiedKey === 'modelName' ? CheckmarkOutline : CopyOutline" />
                </n-button>
              </div>
            </n-descriptions-item>
            <n-descriptions-item label="模型类型">
              {{ modelTypeMap[model.model_type] ?? model.model_type }}
            </n-descriptions-item>
            <n-descriptions-item label="模型描述">
              {{ model.description || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="最大上下文">
              {{ formatContextLength(model.context_length) }}
            </n-descriptions-item>
            <n-descriptions-item label="能力标签">
              <div
                v-if="model.tags.length"
                class="flex flex-wrap gap-[.25rem]"
              >
                <n-tag
                  v-for="tag in model.tags"
                  :key="tag"
                  size="small"
                  type="primary"
                  :bordered="false"
                >
                  {{ tag }}
                </n-tag>
              </div>
              <span v-else>—</span>
            </n-descriptions-item>
            <n-descriptions-item label="创建来源">
              {{ routeOriginMap[model.origin] ?? model.origin }}
            </n-descriptions-item>
            <n-descriptions-item label="失败切换">
              {{ model.failover_enabled ? `已启用(最多切换 ${model.failover_max_retries} 次)` : '未启用' }}
            </n-descriptions-item>
            <n-descriptions-item label="上线时间">
              {{ model.created_at.slice(0, 10) }}
            </n-descriptions-item>
          </n-descriptions>
        </section>

        <!-- 计费方案 -->
        <section>
          <div class="mb-[.5rem] text-sm font-semibold text-strong">
            计费方案
          </div>
          <n-table
            size="small"
            :single-line="false"
          >
            <thead>
              <tr>
                <th>计费维度</th>
                <th>价格</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in billingRows"
                :key="row.dimension"
              >
                <td>{{ row.dimension }}</td>
                <td>{{ row.price }}</td>
                <td>{{ row.desc }}</td>
              </tr>
            </tbody>
          </n-table>
        </section>

        <!-- API 接入 -->
        <section>
          <div class="mb-[.5rem] flex items-center justify-between">
            <span class="text-sm font-semibold text-strong">API接入</span>
            <n-button
              type="primary"
              size="small"
              @click="goApiKey"
            >
              获取API Key
            </n-button>
          </div>
          <div class="flex flex-col gap-[.75rem]">
            <!-- API Base URL:区域核心信息;两协议 SDK 的 baseURL 约定不同,分行展示各自可直贴的地址,各自独立复制 -->
            <div class="flex flex-col gap-[.25rem]">
              <span class="text-sm text-secondary">API Base URL</span>
              <div class="flex flex-col gap-[.5rem]">
                <div
                  v-for="item in apiBaseUrls"
                  :key="item.key"
                  class="flex items-center gap-[.5rem] rounded-md border border-line-strong px-[.75rem] py-[.5rem] bg-fill-4 dark:bg-fill-1"
                >
                  <n-tag
                    size="small"
                    :bordered="false"
                    :color="item.tagColor"
                    class="shrink-0"
                  >
                    {{ item.label }}
                  </n-tag>
                  <code class="flex-1 truncate text-sm font-semibold text-strong">{{ item.value }}</code>
                  <n-button
                    type="primary"
                    size="small"
                    :disabled="!proxyBaseUrl"
                    @click="copyText(item.value, item.key)"
                  >
                    <template #icon>
                      <n-icon :component="copiedKey === item.key ? CheckmarkOutline : CopyOutline" />
                    </template>
                    {{ copiedKey === item.key ? '已复制' : '复制' }}
                  </n-button>
                </div>
              </div>
              <span class="text-base text-tertiary">
                配置 OpenAI / Anthropic SDK 时，请复制对应协议的 Base URL 填入 baseURL，SDK 会自动拼接接口路径。
              </span>
            </div>
            <!-- 支持的接口:方法+路径为辅助信息,视觉层级低于 Base URL -->
            <div class="flex flex-col gap-[.25rem]">
              <span class="text-sm text-secondary">支持的接口</span>
              <div class="flex flex-col gap-[.5rem]">
                <div
                  v-for="item in apiEndpoints"
                  :key="item.key"
                  class="flex items-center gap-[.5rem] rounded-md border border-line-strong px-[.75rem] py-[.5rem] bg-fill-4 dark:bg-fill-1"
                >
                  <n-tag
                    size="small"
                    :bordered="false"
                    :color="item.tagColor"
                    class="shrink-0"
                  >
                    {{ item.label }}
                  </n-tag>
                  <code class="flex-1 text-sm text-default">{{ item.path }}</code>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>
