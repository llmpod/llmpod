import { listClusters } from './cluster'
import { request } from './request'

/** 「稍后再说」跳过标记的 sessionStorage key(守卫与向导页共用) */
export const QUICK_SETUP_SKIP_KEY = 'llmpod:quick-setup:skipped'

/** 「向导已完成」标记的 sessionStorage key(到达完成页时写入,守卫据此防回退) */
export const QUICK_SETUP_DONE_KEY = 'llmpod:quick-setup:done'

/** 「落地引导弹窗」一次性握手标记的 sessionStorage key(守卫写入,BasicLayout 消费后即删) */
export const QUICK_SETUP_PROMPT_KEY = 'llmpod:quick-setup:prompt'

/** 向导是否走 mock(纯前端演示时 VITE_USE_SETUP_MOCK=true;vite/client 类型自带索引签名,无需声明) */
export function isSetupMock(): boolean {
  return import.meta.env.VITE_USE_SETUP_MOCK === 'true'
}

/** mock 内存态:集群是否已创建(会话内有效,守卫检测与向导共用) */
export const mockClusterState = { hasCluster: false }

/** 集群存在性缓存(会话级,避免每次路由跳转都请求) */
let clusterCache: boolean | null = null

/** 是否存在任一集群(守卫空集群检测;mock 模式读内存标记) */
export async function hasAnyCluster(): Promise<boolean> {
  if (isSetupMock()) return mockClusterState.hasCluster
  if (clusterCache !== null) return clusterCache
  const page = await listClusters({ page: 1, page_size: 1 })
  clusterCache = page.total > 0
  return clusterCache
}

/** 集群创建成功后调用,更新缓存与 mock 标记 */
export function markClusterCreated(): void {
  clusterCache = true
  mockClusterState.hasCluster = true
}

/** 缓存失效入口:集群删除/外部变更后调用,强制守卫下次重新探测(同时复位 mock 标记与完成标记,允许重新走向导) */
export function resetClusterCache(): void {
  clusterCache = null
  mockClusterState.hasCluster = false
  sessionStorage.removeItem(QUICK_SETUP_DONE_KEY)
}

/** 向导展示用部署状态(由后端 DeploymentStatus 映射,见 useStore 的 toWizardDeployStatus) */
export type DeployStatus = 'deploying' | 'running' | 'failed'

/** 模型上传结果 */
export interface UploadModelResult {
  id: string
  name: string
  /** 如 "3.2 GB" */
  size_text: string
}

/** 上传模型文件(POST /admin/models/upload) */
export function uploadModel(file: File): Promise<UploadModelResult> {
  const data = new FormData()
  data.append('file', file)
  // http-client 默认注入 Content-Type: application/json,会致 axios 将 FormData 序列化为 JSON 丢失文件字节;
  // 置 null 删除该头由浏览器自动按 multipart 处理(不能手写 'multipart/form-data',缺 boundary 浏览器不会自动补)
  return request({
    url: '/admin/models/upload',
    method: 'POST',
    data,
    headers: { 'Content-Type': null },
  })
}
