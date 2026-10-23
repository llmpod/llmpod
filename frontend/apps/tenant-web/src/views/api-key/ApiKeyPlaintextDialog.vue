<script setup lang="ts">
import { NButton, NIcon, NAlert, NTag } from 'naive-ui'
import { CopyOutline, CheckmarkOutline } from '@vicons/ionicons5'
import { computed, onUnmounted, ref } from 'vue'
import { useDialogClose, message, copyToClipboard } from '@repo/hooks'
import { getProxyEndpoint } from '@/api/proxy-endpoint'

const props = defineProps<{
  /** 新建 Key 的明文（持久可查，列表亦回显） */
  plaintext: string
}>()

const close = useDialogClose()
const copied = ref(false)

/** 复制明文到剪贴板 */
async function copyPlaintext() {
  try {
    await copyToClipboard(props.plaintext)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    message.error('复制失败,请手动复制')
  }
}

/** 数据面接入基址（弹窗打开即拉取；未就绪或失败为 null） */
const proxyBaseUrl = ref<string | null>(null)

void getProxyEndpoint()
  .then((endpoint) => {
    proxyBaseUrl.value = endpoint.base_url.replace(/\/+$/, '')
  })
  .catch(() => {
    // 加载失败保持 null,请求地址展示占位
  })

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

/** 复制成功后短暂显示对勾的标识(请求地址各行独立) */
const copiedKey = ref('')
let copyTimer: ReturnType<typeof setTimeout> | undefined

/** 复制请求地址到剪贴板 */
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
</script>

<template>
  <div class="flex flex-col gap-[1rem] pt-[1rem]">
    <NAlert
      type="warning"
      :show-icon="true"
    >
      Key 明文已持久加密保存，可随时在列表中查看。请立即复制并妥善保管，切勿泄露！
    </NAlert>
    <div class="flex items-center gap-[.5rem]">
      <code class="flex-1 rounded-[.4rem] bg-fill-2 p-[.75rem] font-mono text-sm break-all">
        {{ plaintext }}
      </code>
      <NButton
        :type="copied ? 'primary' : 'default'"
        size="small"
        @click="copyPlaintext"
      >
        <template #icon>
          <NIcon :component="copied ? CheckmarkOutline : CopyOutline" />
        </template>
        {{ copied ? '已复制' : '复制' }}
      </NButton>
    </div>
    <!-- API Base URL:两协议 SDK 的 baseURL 约定不同,分行展示各自可直贴的地址,各自独立复制 -->
    <div class="flex flex-col gap-[.25rem]">
      <span class="text-sm text-secondary">API Base URL</span>
      <div class="flex flex-col gap-[.5rem]">
        <div
          v-for="item in apiBaseUrls"
          :key="item.key"
          class="flex items-center gap-[.5rem] rounded-[.4rem] border border-line-strong px-[.75rem] py-[.5rem] bg-fill-4 dark:bg-fill-1"
        >
          <NTag
            size="small"
            :bordered="false"
            :color="item.tagColor"
            class="shrink-0"
          >
            {{ item.label }}
          </NTag>
          <code class="flex-1 truncate font-mono text-sm font-semibold text-strong">{{ item.value }}</code>
          <NButton
            type="primary"
            size="small"
            :disabled="!proxyBaseUrl"
            @click="copyText(item.value, item.key)"
          >
            <template #icon>
              <NIcon :component="copiedKey === item.key ? CheckmarkOutline : CopyOutline" />
            </template>
            {{ copiedKey === item.key ? '已复制' : '复制' }}
          </NButton>
        </div>
      </div>
      <span class="text-base text-tertiary">
        配置 OpenAI / Anthropic SDK 时，请复制对应协议的 Base URL 填入 baseURL，SDK 会自动拼接接口路径。
      </span>
    </div>
    <!-- 支持的接口:辅助信息,视觉层级低于 Base URL -->
    <div class="flex flex-col gap-[.25rem]">
      <span class="text-sm text-secondary">支持的接口</span>
      <div class="flex flex-col gap-[.5rem]">
        <div
          v-for="item in apiEndpoints"
          :key="item.key"
          class="flex items-center gap-[.5rem] rounded-[.4rem] border border-line-strong px-[.75rem] py-[.5rem] bg-fill-4 dark:bg-fill-1"
        >
          <NTag
            size="small"
            :bordered="false"
            :color="item.tagColor"
            class="shrink-0"
          >
            {{ item.label }}
          </NTag>
          <code class="flex-1 font-mono text-sm text-default">{{ item.path }}</code>
        </div>
      </div>
    </div>
    <div class="flex justify-end">
      <NButton
        type="primary"
        @click="close"
      >
        我已复制保存
      </NButton>
    </div>
  </div>
</template>
