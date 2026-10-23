import {
  PLAYGROUND_CHAT_MODEL_TYPES,
  createChatSender,
  toOpenAIBaseURL,
  type PlaygroundApiKeyOption,
  type PlaygroundModelOption,
} from '@repo/naive-ui'
import { useDialog, message } from '@repo/hooks'
import { listTenantKeys } from '@/api/api-key'
import { getProxyEndpoint } from '@/api/proxy-endpoint'
import { listTenantModels } from '@/api/model'
import ApiKeyForm from '@/views/api-key/ApiKeyForm.vue'
import { useStore as useApiKeyStore } from '@/views/api-key/useStore'

export function useController() {
  const models = ref<PlaygroundModelOption[]>([])
  /** 路由携带的初始模型(模型市场「在线调试」跳转);不在可选列表时由组件回退第一个 */
  const route = useRoute()
  const initialModel = typeof route.query.model === 'string' ? route.query.model : null
  const apiKeys = ref<PlaygroundApiKeyOption[]>([])
  /** 当前选中的 API Key(key id 字符串,v-model 绑定到组件下拉) */
  const apiKey = ref<string | null>(null)
  /** key id -> 明文,发送时按当前选中解析(明文只留在闭包,不进模板) */
  const keyPlaintexts = new Map<string, string>()
  /** 数据面 OpenAI 兼容 baseURL(= proxy base_url + /v1,端点加载前为空) */
  const proxyBaseURL = ref('')
  /** 本租户全量模型别名(创建 Key 弹窗授权模型候选,不按可对话类别过滤) */
  const modelAliases = ref<string[]>([])

  // 直连数据面:端点与 API Key 均按发送时刻动态解析(用户可随时切换 Key)
  const sender = createChatSender({
    baseURL: () => proxyBaseURL.value,
    apiKey: () => (apiKey.value ? keyPlaintexts.get(apiKey.value) : undefined),
  })

  /** 拉取可用 Key 列表:仅启用状态的 key 可用于调试,label 带明文尾号区分同名 */
  async function loadKeys() {
    try {
      const keys = (await listTenantKeys()).filter((key) => key.status === 'active')
      apiKeys.value = keys.map((key) => ({
        label: `${key.name}(···${key.plaintext.slice(-4)})`,
        value: String(key.id),
      }))
      keyPlaintexts.clear()
      for (const key of keys) keyPlaintexts.set(String(key.id), key.plaintext)
    } catch {
      message.error('API Key 列表加载失败,请稍后重试')
    }
  }

  // 订阅 api-key 数据变更:签发/吊销/调整完成后自动刷新 Key 下拉
  // (选中项回退/默认选第一个由 LmPlayground 对 apiKeys 的 watch 处理)
  const unsubscribeKeys = useApiKeyStore().subscribe(() => {
    void loadKeys()
  })
  onBeforeUnmount(unsubscribeKeys)

  /** 创建 API Key 弹窗:复用 api-key 页签发表单,创建完成经 store notify 自动刷新列表 */
  function openCreateApiKey() {
    useDialog({
      title: '创建 API Key',
      component: ApiKeyForm,
      props: { modelAliases: modelAliases.value },
      area: 44,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  onMounted(async () => {
    await loadKeys()

    // 数据面端点:base_url 不含版本段,补 /v1 作为 OpenAI SDK baseURL
    try {
      proxyBaseURL.value = toOpenAIBaseURL((await getProxyEndpoint()).base_url)
    } catch {
      message.error('数据面接入地址加载失败,请稍后重试')
    }

    // 模型列表:本租户可用全集(已发布 − 封禁)中可对话调试的类别(llm/multimodal);
    // 全量别名另存,供创建 Key 弹窗的授权模型候选
    try {
      const routes = await listTenantModels()
      models.value = routes
        .filter((route) => PLAYGROUND_CHAT_MODEL_TYPES.includes(route.model_type))
        .map((route) => ({
          label: route.name,
          value: route.name,
          // 模型元数据:切换模型时自动填充思考强度
          reasoning: route.tags.includes('思考'),
        }))
      modelAliases.value = routes.map((route) => route.name)
    } catch {
      message.error('模型列表加载失败,请稍后重试')
    }
  })

  return { models, apiKeys, apiKey, sender, openCreateApiKey, initialModel }
}
