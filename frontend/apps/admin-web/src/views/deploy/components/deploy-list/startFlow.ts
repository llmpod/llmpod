import { isHttpError } from '@repo/http-client'
import { useConfirm, message } from '@repo/hooks'
import { useStore, type DeployRecord } from './useStore'

/**
 * 启动(重启)部署共享交互流(列表行操作与详情头部共用):
 * 确认 → 调接口;原卡被其他部署占用(409)时提示占用信息并二次确认强制启动(force=true);
 * 成功返回 true(写操作内部已 notify 联动列表),取消/失败返回 false。
 * start 走 silentError,错误提示统一在此处理。
 */
export async function startDeploymentFlow(
  row: Pick<DeployRecord, 'deployment_id' | 'name'>,
): Promise<boolean> {
  const { start } = useStore()
  try {
    await useConfirm({
      title: '启动部署',
      content: `确定启动部署「${row.name}」吗?将在原卡重新预留显存并重新拉起实例。`,
      confirmText: '启动',
      cancelText: '取消',
    })
    await start(row.deployment_id)
    message.success(`已启动:${row.name}`)
    return true
  } catch (error) {
    // 取消(非 HttpError)静默;非 409 的接口错误手动提示
    if (!isHttpError(error)) return false
    if (error.status !== 409) {
      message.error(error.message)
      return false
    }
    // 409:原卡被其他部署占用,二次确认后强制抢占(自负 OOM 风险)
    try {
      await useConfirm({
        title: '强制启动',
        content: `${error.message}。可强制启动抢占原卡(自负 OOM 风险),是否继续?`,
        confirmText: '强制启动',
        cancelText: '取消',
      })
      await start(row.deployment_id, true)
      message.success(`已启动:${row.name}`)
      return true
    } catch (forceError) {
      // 取消静默;强制启动仍失败时手动提示
      if (isHttpError(forceError)) message.error(forceError.message)
      return false
    }
  }
}
