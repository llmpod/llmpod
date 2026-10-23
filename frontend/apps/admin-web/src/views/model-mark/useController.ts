import { onMounted } from 'vue'
import { useStore, type RouteData } from './useStore'

export function useController() {
  const { records, loading, fetchModels, fetchProxyEndpoint } = useStore()
  const router = useRouter()

  /** 关键词搜索(按模型名本地过滤,接口无搜索参数) */
  const keyword = ref('')

  onMounted(() => {
    void fetchModels()
    // 数据面接入地址:与模型列表并行拉取,失败静默
    void fetchProxyEndpoint()
  })

  const filtered = computed(() => {
    const text = keyword.value.trim().toLowerCase()
    if (!text) return records.value
    return records.value.filter((model) => model.name.toLowerCase().includes(text))
  })

  /** 当前查看的模型 + 详情抽屉显隐 */
  const activeModel = ref<RouteData | null>(null)
  const detailDrawerShow = ref(false)

  /** 点击卡片:打开模型详情抽屉 */
  function openDetail(model: RouteData) {
    activeModel.value = model
    detailDrawerShow.value = true
  }

  /** 在线调试:跳转 Playground,携带模型名供动态加载 */
  function goPlayground(model: RouteData) {
    router.push({ name: 'playground', query: { model: model.name } })
  }

  return {
    keyword,
    loading,
    filtered,
    openDetail,
    goPlayground,
    activeModel,
    detailDrawerShow,
  }
}
