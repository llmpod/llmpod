<script setup lang="ts">
import { CheckmarkCircleOutline } from '@vicons/ionicons5'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/store/auth'
import { useStore } from '../useStore'

const { cluster, onlineNodes, model, deploy } = useStore()
const router = useRouter()
const auth = useAuthStore()

/** 跳转指定菜单页;无权限时退回首个有权限菜单 */
function go(name: string, query?: Record<string, string>) {
  if (auth.hasMenu(name)) {
    void router.replace({ name, query })
    return
  }
  const first = auth.firstMenuName()
  void router.replace(first ? { name: first } : { path: '/' })
}

/** 「查看部署详情」跳转参数:进入部署列表页签并直达刚创建部署的详情视图;未拿到部署时退回列表 */
const detailQuery = computed<Record<string, string>>(() => {
  const query: Record<string, string> = { tab: 'list' }
  if (deploy.value) {
    query.action = 'detail'
    query.id = deploy.value.id
  }
  return query
})
</script>

<template>
  <div class="flex flex-col items-center gap-[1.6rem]">
    <n-icon :component="CheckmarkCircleOutline" :size="56" class="text-success" />
    <div class="text-[1.8rem] font-semibold text-strong">一键部署完成</div>

    <div
      class="w-full max-w-[68rem] rounded-lg bg-fill-2 p-[1.6rem] flex flex-col gap-[.8rem] text-base"
    >
      <div class="flex justify-between">
        <span class="text-secondary">集群</span>
        <span class="text-default">{{ cluster?.name ?? '—' }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">在线节点</span>
        <span class="text-default">{{ onlineNodes.length }} 个</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">模型</span>
        <span class="text-default">{{ model?.name ?? '（稍后上传）' }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">部署</span>
        <n-tag
          v-if="deploy"
          size="small"
          :bordered="false"
          :type="deploy.status === 'running' ? 'success' : 'warning'"
        >
          {{ deploy.name }} · {{ deploy.status === 'running' ? '运行中' : '部署中' }}
        </n-tag>
        <span v-else class="text-default">—</span>
      </div>
    </div>

    <div class="flex gap-[1rem]">
      <n-button type="primary" @click="go('deploy', detailQuery)"> 查看部署详情 </n-button>
      <n-button @click="go('model-release', { tab: 'release' })"> 查看发布列表 </n-button>
      <n-button quaternary @click="go('model-mark')"> 前往模型市场 </n-button>
    </div>
  </div>
</template>
