import { request } from './request'
import type {
  CreateDeploymentPayload,
  DeploymentDetailOut,
  DeploymentPageData,
  DeploymentStatus,
  DeploymentTopology,
  DeployLogPageData,
  DeployNodeSummaryItem,
  InspectDeploymentPayload,
  InspectDeploymentResult,
  InstanceCommand,
  ListDeploymentLogsParams,
  ListDeploymentsParams,
  ModelDeploymentOut,
  NormalizedTopology,
  ScalePreviewOut,
} from './deployment.types'

export * from './deployment.types'

/**
 * 部署域调用层:预检 / 创建 / 列表 / 详情 / 日志 / 启停 / 扩缩,
 * 以及两个纯数据换算(拓扑归一、副本序号),供视图层直接复用。
 */

/** 拓扑归一化(纯函数):两种写法(units×copies / 旧四数)统一为全字段有值的形状;copies 的异构 list 形态按列表长归一(OAS:份数 = 列表长) */
export function normalizedTopology(
  topology: Partial<DeploymentTopology> | null | undefined,
): NormalizedTopology {
  const units =
    topology?.units?.length
      ? topology.units
      : [
          {
            role: 'full',
            tp: topology?.tp ?? 1,
            pp: topology?.pp ?? 1,
            ep: topology?.ep ?? 1,
            nnodes: topology?.nnodes ?? 1,
          },
        ]
  const rawCopies = topology?.copies
  const copies = Array.isArray(rawCopies) ? rawCopies.length : (rawCopies ?? topology?.replicas ?? 1)
  const first = units[0]
  return {
    units,
    copies,
    gpusPerEngine: first.tp * first.pp * first.ep,
    maxModelLen: topology?.max_model_len ?? null,
  }
}

/** 部署预检(POST /api/v1/admin/model-deployments/inspect):推荐配置 + 校验 + 逐卡评估 */
export function inspectDeployment(data: InspectDeploymentPayload): Promise<InspectDeploymentResult> {
  return request({ url: '/admin/model-deployments/inspect', method: 'POST', data })
}

/** 创建部署(POST /api/v1/admin/model-deployments):落库 status=creating 并唤醒控制循环,立即返回;结果靠轮询 */
export function createDeployment(data: CreateDeploymentPayload): Promise<ModelDeploymentOut> {
  return request({ url: '/admin/model-deployments', method: 'POST', data })
}

/**
 * 副本序号 0 基 → 人读 1 基(全站统一收口:后端 seq 为 0..copies-1,展示为「副本N」从 1 起;
 * 推导路径(缺 replicas 层)按 index+1 生成,两条路径经此函数对齐)。
 */
export function toReplicaSeq(seq: number): number {
  return seq + 1
}

/** 列出部署(GET /api/v1/admin/model-deployments):分页 + cluster_id/status 过滤,前端据此轮询状态 */
export function listDeployments(
  params: ListDeploymentsParams,
  signal?: AbortSignal,
): Promise<DeploymentPageData> {
  return request({ url: '/admin/model-deployments', method: 'GET', params, signal })
}

/** 部署详情(GET /api/v1/admin/model-deployments/{deployment_id}):deployment + instances + endpoint */
export function getDeployment(deploymentId: string): Promise<DeploymentDetailOut> {
  return request({ url: `/admin/model-deployments/${deploymentId}`, method: 'GET' })
}

/** 实例启动命令(GET /admin/model-deployments/{id}/instances/{iid}/command):该容器实际下发的完整 docker run */
export function getInstanceCommand(
  deploymentId: string,
  instanceId: string,
): Promise<InstanceCommand> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/instances/${instanceId}/command`,
    method: 'GET',
  })
}

/** 部署每节点当前步骤/状态(GET /admin/model-deployments/{id}/logs/nodes):拓扑节点徽标数据 */
export function getDeploymentNodeSummary(deploymentId: string): Promise<DeployNodeSummaryItem[]> {
  return request({ url: `/admin/model-deployments/${deploymentId}/logs/nodes`, method: 'GET' })
}

/** 部署全流程日志(GET /admin/model-deployments/{id}/logs):结构化时间线,升序分页 + since 增量 */
export function listDeploymentLogs(
  deploymentId: string,
  params?: ListDeploymentLogsParams,
  signal?: AbortSignal,
): Promise<DeployLogPageData> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/logs`,
    method: 'GET',
    params,
    signal,
  })
}

/** 停止部署(POST .../stop):可停状态 running/failed/creating,返回 { deployment_id, status: 'stopped' } */
export function stopDeployment(
  deploymentId: string,
): Promise<{ deployment_id: string; status: DeploymentStatus }> {
  return request({ url: `/admin/model-deployments/${deploymentId}/stop`, method: 'POST' })
}

/**
 * 启动(重启)部署(POST .../start):仅 stopped/failed 可启动,原卡重新预留 VRAM 后重部署;
 * 原卡被其他部署占用返回 409(data.occupied 明细),force=true 强制抢占(自负 OOM 风险)。
 * silentError:409 占用需前端二次确认强制启动,错误提示由调用方统一处理。
 */
export function startDeployment(
  deploymentId: string,
  force = false,
): Promise<Record<string, unknown>> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/start`,
    method: 'POST',
    params: force ? { force: true } : undefined,
    meta: { silentError: true },
  })
}

/** 删除部署(POST .../delete):标 deleting → 停容器 → 级联删实例/端点/部署,返回 { deployment_id, deleted: true } */
export function deleteDeployment(
  deploymentId: string,
): Promise<{ deployment_id: string; deleted: boolean }> {
  return request({ url: `/admin/model-deployments/${deploymentId}/delete`, method: 'POST' })
}

/** 扩缩预览(POST /admin/model-deployments/{id}/scale/preview):可扩上限/空闲卡/建议落点 */
export function scalePreview(deploymentId: string): Promise<ScalePreviewOut> {
  return request({ url: `/admin/model-deployments/${deploymentId}/scale/preview`, method: 'POST' })
}

/** 在线扩缩(POST /admin/model-deployments/{id}/scale):即时受理,进度靠轮询(phase 转入 SCALING);
 *  仅 running 可调;SCALING 中或目标=当前返回 409(错误提示走 HTTP 层默认行为) */
export function scaleDeployment(
  deploymentId: string,
  copies: number,
): Promise<{ deployment_id: string; copies: number; status: string }> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/scale`,
    method: 'POST',
    data: { copies },
  })
}
