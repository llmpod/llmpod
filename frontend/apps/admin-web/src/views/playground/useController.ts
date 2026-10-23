import {
  PLAYGROUND_CHAT_MODEL_TYPES,
  createChatSender,
  type PlaygroundModelOption,
} from '@repo/naive-ui'
import { message } from '@repo/hooks'
import { useAuthStore } from '@/store/auth'
import { listPublishes } from '@/api/publish'

export function useController() {
  const models = ref<PlaygroundModelOption[]>([])
  /** 路由携带的初始模型(模型市场「在线调试」跳转);不在可选列表时由组件回退第一个 */
  const route = useRoute()
  const initialModel = typeof route.query.model === 'string' ? route.query.model : null

  // 管理端直连控制面本地转发端点:会话 token 鉴权(非 API Key),不取数据面地址
  const sender = createChatSender({
    baseURL: () => `${window.location.origin}/api/v1/admin/inference`,
    getToken: () => useAuthStore().token,
  })

  onMounted(async () => {
    // 模型列表:发布路由中可对话调试的类别(llm/multimodal),取对外模型名
    try {
      const publishes = await listPublishes()
      models.value = publishes
        .filter(
          (item) =>
            item.route.status === 'published' &&
            PLAYGROUND_CHAT_MODEL_TYPES.includes(item.route.model_type),
        )
        .map((item) => ({
          label: item.route.name,
          value: item.route.name,
          // 模型元数据:切换模型时自动填充思考强度
          reasoning: item.route.tags.includes('思考'),
        }))
    } catch {
      message.error('模型列表加载失败,请稍后重试')
    }
  })

  return { models, sender, initialModel }
}
