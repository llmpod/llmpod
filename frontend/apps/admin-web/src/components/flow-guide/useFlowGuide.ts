import { UIAutomator } from '@repo/ui-automator'

/** 部署流程引导步骤:key 与目标页面入口按钮的 data-flow-guide 属性一一对应 */
export interface FlowGuideStep {
  key: string
  label: string
  routeName: string
  /** 目标页面入口按钮文案,用于 tooltip 提示 */
  actionText: string
  /** 跳转目标路由时附加的 query,用于定位 deploy 页等子 tab;不填则不带 */
  query?: Record<string, string>
}

export const FLOW_GUIDE_STEPS: FlowGuideStep[] = [
  { key: 'create-node-pool', label: '创建集群', routeName: 'node-pool', actionText: '创建集群' },
  { key: 'register-node', label: '注册节点', routeName: 'node-list', actionText: '注册节点' },
  {
    key: 'upload-model',
    label: '添加模型',
    routeName: 'model-management',
    actionText: '上传模型',
  },
  {
    key: 'create-deploy',
    label: '新建部署',
    routeName: 'deploy',
    actionText: '新建部署',
    // 「新建部署」按钮位于部署列表(list)tab;deploy 页默认 tab 已改为模型管理,需显式定位
    query: { tab: 'list' },
  },
  // { key: 'release-model', label: '模型发布', routeName: 'model-release', actionText: '部署大模型' },
]

// 引导状态提升为模块级单例:引导伴随路由跳转,源页面组件卸载后引导仍需继续执行
const runningKey = ref<string | null>(null)
let activeBot: UIAutomator | null = null

export function useFlowGuide() {
  const router = useRouter()

  /** 点击流程步骤:跳转目标路由 → 等待入口按钮渲染 → 模拟鼠标移入并点击,自动打开对应表单 */
  async function startGuide(step: FlowGuideStep) {
    if (runningKey.value) return
    runningKey.value = step.key
    try {
      // 已在目标页时 push 同名路由会抛冗余导航错误,静默忽略;query 用于落到正确的子 tab
      await router.push({ name: step.routeName, query: step.query }).catch(() => {})
      const selector = `[data-flow-guide="${step.key}"]`
      activeBot = new UIAutomator({ mouseSpeed: 1.5, maxRetries: 2 })
      await activeBot
        .waitFor(selector, { timeout: 5000 })
        .tooltip(selector, `点击${step.label}`, 1000)
        .click(selector)
        .run()
    } catch {
      // 目标元素未出现或引导被中断时静默结束,不影响页面正常使用
    } finally {
      runningKey.value = null
      activeBot = null
    }
  }

  return { steps: FLOW_GUIDE_STEPS, runningKey, startGuide }
}
