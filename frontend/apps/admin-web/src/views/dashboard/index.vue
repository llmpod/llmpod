<script setup lang="ts">
import { useController } from './useController'

const {
  loading,
  timeText,
  dateText,
  banner,
  bannerTitleText,
  bannerDetailText,
  kpiCards,
  trendEl,
  trendTotalsText,
  upstreamCards,
  upstreamAttentionCount,
  serviceInfo,
  nodesInfo,
  gpuInfo,
  gpuPools,
  deploySummary,
  deployList,
  canDeploy,
  canNodePool,
  goDeployStatus,
  goDeployDetail,
  goClusterDetail,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 健康横幅 + 实时时钟(§7.3 顶栏;横幅为前端自算,§8 #2) -->
    <LmCard>
      <template v-if="loading">
        <div class="flex items-center justify-between flex-wrap gap-[.6rem]">
          <n-skeleton text style="width: 16rem; font-size: 14px; margin: 4.5px 0" />
          <div class="flex flex-col items-end">
            <n-skeleton text style="width: 9rem; font-size: 18px; margin: 5.4px 0" />
            <n-skeleton text style="width: 7rem; font-size: 12px; margin: 3.6px 0" />
          </div>
        </div>
      </template>
      <template v-else>
        <div class="flex items-center justify-between flex-wrap gap-y-[.6rem]">
          <div class="flex items-center gap-[.6rem] flex-wrap min-w-0">
            <span class="dash-dot shrink-0" :class="banner.ok ? 'dash-dot--ok' : 'dash-dot--down'" />
            <span class="text-base font-semibold">{{ bannerTitleText }}</span>
            <span v-if="bannerDetailText" class="dash-subtitle truncate">{{ bannerDetailText }}</span>
          </div>
          <div class="text-right shrink-0">
            <div class="dash-time">{{ timeText }}</div>
            <div class="dash-subtitle">{{ dateText }}</div>
          </div>
        </div>
      </template>
    </LmCard>

    <!-- KPI 六卡:窄屏 2 列起步,逐级升到 6 列 -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-3 xl:grid-cols-6">
      <template v-if="loading">
        <LmCard v-for="i in 6" :key="i">
          <n-skeleton text style="display: block; width: 6rem; font-size: 12px; margin-bottom: 7.2px" />
          <n-skeleton text style="display: block; width: 7.2rem; font-size: 20px; margin-bottom: 7.2px" />
          <n-skeleton text style="display: block; width: 5.4rem; font-size: 12px" />
        </LmCard>
      </template>
      <template v-else>
        <n-empty
          v-if="!kpiCards.length"
          description="暂无指标数据"
          class="col-span-2 md:col-span-3 xl:col-span-6 py-[2rem]"
        />
        <LmCard v-for="card in kpiCards" :key="card.label">
          <div class="dash-subtitle">{{ card.label }}</div>
          <div class="dash-stat-value">{{ card.value }}</div>
          <div
            v-if="card.deltaText"
            class="dash-delta"
            :class="card.deltaTone === 'bad' ? 'dash-delta--bad' : card.deltaTone === 'good' ? 'dash-delta--good' : ''"
          >
            {{ card.deltaText }}
          </div>
          <div v-if="card.note" class="dash-subtitle mt-[.2rem]">{{ card.note }}</div>
        </LmCard>
      </template>
    </div>

    <!-- 服务状态 · 实时(§7.3:集群 / 部署 / 在线 三张卡;宽屏并排,窄屏逐级堆叠) -->
    <div class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2 xl:grid-cols-3">
      <template v-if="loading">
        <LmCard v-for="i in 3" :key="i">
          <n-skeleton text style="width: 5rem; font-size: 14px; margin: 4.2px 0 12px" />
          <div class="grid grid-cols-2 gap-[.75rem]">
            <n-skeleton v-for="j in 2" :key="j" :sharp="false" style="height: 4.8rem" />
          </div>
          <n-skeleton
            v-for="k in 2"
            :key="k"
            :sharp="false"
            style="height: 3.6rem; margin-top: 7.5px"
          />
        </LmCard>
      </template>

      <template v-else>
        <!-- ① 集群信息:节点 / GPU 在线规模 + GPU 集群池 -->
        <LmCard>
          <div class="dash-card-header">
            <span class="text-small font-semibold text-strong">集群信息</span>
          </div>
          <div class="grid grid-cols-2 gap-[.75rem]">
            <div class="dash-tile">
              <span class="text-small text-secondary">节点在线</span>
              <span
                class="dash-tile-value"
                :class="nodesInfo.ok ? 'text-strong' : 'dash-text--warning'"
              >{{ nodesInfo.online }}/{{ nodesInfo.total }}</span>
            </div>
            <div class="dash-tile">
              <span class="text-small text-secondary">GPU 在线</span>
              <span
                class="dash-tile-value"
                :class="gpuInfo.ok ? 'text-strong' : 'dash-text--warning'"
              >{{ gpuInfo.online }}/{{ gpuInfo.total }}</span>
            </div>
          </div>
          <div class="dash-scroll dash-scroll--tall mt-[.75rem] flex flex-col gap-[.75rem]">
            <div
              v-for="pool in gpuPools"
              :key="pool.id"
              :class="canNodePool ? 'dash-pool dash-pool--link' : 'dash-pool'"
              :role="canNodePool ? 'button' : undefined"
              :tabindex="canNodePool ? 0 : undefined"
              @click="canNodePool && goClusterDetail(pool.id)"
              @keyup.enter="canNodePool && goClusterDetail(pool.id)"
            >
              <div class="flex items-center justify-between gap-[.5rem]">
                <span class="min-w-0 truncate">{{ pool.name }}</span>
                <n-tag size="small" :bordered="false" class="dash-tag--purple shrink-0">
                  {{ pool.gpuCount }} GPU
                </n-tag>
              </div>
              <div class="flex items-center gap-[.5rem] w-full">
                <n-progress
                  type="line"
                  class="flex-1"
                  :percentage="pool.percent ?? 0"
                  :show-indicator="false"
                  :height="6"
                  :border-radius="3"
                  :color="pool.hot ? 'rgb(var(--n-warning-color))' : 'rgb(var(--n-primary-color))'"
                />
                <span
                  class="dash-subtitle w-[3rem] shrink-0 text-right whitespace-nowrap"
                  :title="pool.percent === null ? '无实时数据' : ''"
                >{{ pool.percent === null ? '—' : `${pool.percent}%` }}</span>
              </div>
              <div class="dash-subtitle">{{ pool.meta }}</div>
            </div>
            <n-empty v-if="!gpuPools.length" size="small" description="暂无集群" class="!py-[1rem]" />
          </div>
        </LmCard>

        <!-- ② 部署信息:四态计数(可跳转筛选)+ 最近部署 -->
        <LmCard>
          <div class="dash-card-header">
            <span class="text-small font-semibold text-strong">部署信息</span>
          </div>
          <div class="grid grid-cols-2 gap-[.75rem]">
            <div
              v-for="item in deploySummary"
              :key="item.label"
              :class="canDeploy ? 'dash-tile dash-tile--link' : 'dash-tile'"
              :role="canDeploy ? 'button' : undefined"
              :tabindex="canDeploy ? 0 : undefined"
              @click="canDeploy && goDeployStatus(item.status)"
              @keyup.enter="canDeploy && goDeployStatus(item.status)"
            >
              <span class="text-small text-secondary">{{ item.label }}</span>
              <span
                class="dash-tile-value"
                :class="item.tone === 'default' ? 'text-strong' : `dash-text--${item.tone}`"
              >{{ item.value }}</span>
            </div>
          </div>
          <div class="mt-[.75rem] flex flex-col min-h-0">
            <div class="dash-subtitle">最近部署</div>
            <div class="dash-scroll flex-1 min-h-0">
              <div v-for="deploy in deployList" :key="deploy.id" class="dash-list-row">
                <span
                  :class="canDeploy
                    ? 'flex-1 min-w-0 truncate text-primary cursor-pointer hover:opacity-[0.9]'
                    : 'flex-1 min-w-0 truncate'"
                  :role="canDeploy ? 'button' : undefined"
                  :tabindex="canDeploy ? 0 : undefined"
                  @click="canDeploy && goDeployDetail(deploy.id)"
                  @keyup.enter="canDeploy && goDeployDetail(deploy.id)"
                >{{ deploy.name }}</span>
                <n-tag size="small" :type="deploy.statusType" :bordered="false">
                  {{ deploy.statusLabel }}
                </n-tag>
              </div>
            </div>
            <n-empty v-if="!deployList.length" size="small" description="暂无部署" class="!py-[1rem]" />
          </div>
        </LmCard>

        <!-- ③ 在线信息:在线模型 / 活跃副本 + 引擎分布 -->
        <LmCard>
          <div class="dash-card-header">
            <span class="text-small font-semibold text-strong">在线信息</span>
          </div>
          <div class="grid grid-cols-2 gap-[.75rem]">
            <div class="dash-tile">
              <span class="text-small text-secondary">在线模型</span>
              <span class="dash-tile-value text-strong">{{ serviceInfo.onlineModels }}</span>
            </div>
            <div class="dash-tile">
              <span class="text-small text-secondary">活跃副本</span>
              <span class="dash-tile-value text-strong">{{ serviceInfo.replicas }}</span>
            </div>
          </div>
          <div class="mt-[.75rem] flex flex-col min-h-0">
            <div class="dash-subtitle">引擎分布</div>
            <div class="dash-scroll flex-1 min-h-0">
              <div v-for="item in serviceInfo.engineMix" :key="item.engine" class="dash-list-row">
                <span class="flex-1 min-w-0 truncate">{{ item.engine }}</span>
                <span class="tabular-nums text-secondary">{{ item.count }}</span>
              </div>
            </div>
            <n-empty
              v-if="!serviceInfo.engineMix.length"
              size="small"
              description="暂无活跃实例"
              class="!py-[1rem]"
            />
          </div>
        </LmCard>
      </template>
    </div>

    <!-- 调用趋势(今日 vs 昨日同期)+ 上游依赖:窄屏纵向堆叠,≥lg 2:1 -->
    <div class="grid grid-cols-1 gap-[.75rem] lg:grid-cols-3">
      <LmCard class="lg:col-span-2">
        <div class="dash-card-header flex-wrap gap-y-[.4rem]">
          <span class="dash-card-title">调用趋势 · 今日 vs 昨日同期</span>
          <span class="dash-subtitle">{{ trendTotalsText }}</span>
        </div>
        <n-skeleton v-if="loading" :sharp="false" style="width: 100%; height: 300px" />
        <div v-show="!loading" ref="trendEl" class="dash-chart" />
      </LmCard>

      <LmCard :content-style="{ display: 'flex', flexDirection: 'column' }">
        <div class="dash-card-header">
          <span class="dash-card-title">上游依赖 · 今日</span>
          <span v-if="upstreamAttentionCount > 0" class="dash-text--warning text-small">
            {{ upstreamAttentionCount }} 项需关注
          </span>
        </div>
        <template v-if="loading">
          <div v-for="i in 4" :key="i" class="dash-upstream-row">
            <n-skeleton text style="width: 5rem; font-size: 13px; margin: 4.5px 0" />
            <n-skeleton :sharp="false" style="width: 100%; height: 8px; margin: 8px 0" />
            <n-skeleton text style="width: 6rem; font-size: 12px; margin: 4.5px 0" />
          </div>
        </template>
        <template v-else-if="upstreamCards">
          <div
            v-for="item in [upstreamCards.local, upstreamCards.external]"
            :key="item.name"
            class="dash-upstream-row"
          >
            <div class="flex items-center justify-between">
              <span class="text-small">{{ item.name }}</span>
              <span class="text-small tabular-nums">
                成功率 {{ item.successText }}
                <span v-if="item.attention" class="dash-text--warning">!</span>
              </span>
            </div>
            <div class="flex items-center justify-between gap-[.5rem]">
              <n-progress
                type="line"
                class="flex-1"
                :percentage="item.sharePct"
                :show-indicator="false"
                :height="8"
                :border-radius="4"
              />
              <span class="text-small tabular-nums text-right ml-[1rem]">{{ item.shareText }}</span>
            </div>
          </div>
          <div v-if="upstreamCards.providers.length" class="dash-upstream-divider" />
          <div
            v-for="provider in upstreamCards.providers"
            :key="provider.name"
            class="dash-upstream-row"
          >
            <div class="flex items-center justify-between">
              <span class="text-small truncate">{{ provider.name }}</span>
              <span class="text-small tabular-nums shrink-0">
                成功率 {{ provider.successText }}
                <span v-if="provider.attention" class="dash-text--warning">!</span>
              </span>
            </div>
            <div class="flex items-center justify-between gap-[.5rem]">
              <n-progress
                type="line"
                class="flex-1"
                :percentage="provider.sharePct"
                :show-indicator="false"
                :height="8"
                :border-radius="4"
                color="rgb(var(--n-primary-color))"
              />
              <span class="text-small tabular-nums text-right ml-[1rem]">{{ provider.shareText }}</span>
            </div>
          </div>
          <n-empty
            v-if="!upstreamCards.providers.length"
            size="small"
            description="暂无外部供应商流量"
            class="flex-1 justify-center"
          />
        </template>
        <n-empty v-else description="暂无上游依赖数据" class="flex-1 justify-center" />
      </LmCard>
    </div>
  </div>
</template>

<style scoped>
.dash-subtitle {
  font-size: 12px;
  color: var(--c-secondary);
}

.dash-time {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

.dash-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.dash-dot--ok {
  background-color: rgb(var(--n-success-color));
}

.dash-dot--down {
  background-color: rgb(var(--n-warning-color));
}

.dash-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--c-strong);
}

.dash-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.dash-stat-value {
  font-size: 20px;
  font-weight: 600;
  margin: 4px 0;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

/* 环比行:中性=次色,good/bad 走语义色 */
.dash-delta {
  font-size: 12px;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-secondary);
}

.dash-delta--good {
  color: rgb(var(--n-success-color));
}

.dash-delta--bad {
  color: rgb(var(--n-error-color));
}

.dash-text--success {
  color: rgb(var(--n-success-color));
}

.dash-text--warning {
  color: rgb(var(--n-warning-color));
}

.dash-text--error {
  color: rgb(var(--n-error-color));
}

/* 紫色标签:GPU 数量 */
.dash-tag--purple {
  background-color: var(--c-tag-purple);
  color: rgb(var(--n-primary-color));
}

/* 趋势图:固定高度(矮身美观优先,不随大屏拉伸) */
.dash-chart {
  width: 100%;
  height: 30rem;
}

/* 上游构成条目 */
.dash-upstream-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 0;
  border-bottom: 1px solid var(--c-line);
}

.dash-upstream-row:last-child {
  border-bottom: none;
}

.dash-upstream-divider {
  border-top: 1px solid var(--c-line-strong);
  margin: 4px 0;
}

/* 三段内的统计磁贴:集群在线规模 / 部署四态 / 在线规模共用一套视觉 */
.dash-tile {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 14px 8px;
  border: 1px solid var(--c-line-strong);
  border-radius: 8px;
  text-align: center;
}

.dash-tile--link {
  cursor: pointer;
  transition: border-color 0.2s ease;
}

.dash-tile--link:hover {
  border-color: rgb(var(--n-primary-color));
}

/* 磁贴数值不写 color:默认色由 text-strong、语义色由 dash-text--* 决定 */
.dash-tile-value {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

/* 三段内的明细行(最近部署 / 引擎分布共用) */
.dash-list-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--c-line);
  font-size: 13px;
}

.dash-list-row:last-child {
  border-bottom: none;
}

/* 列表滚动区:数据过多时限高内部滚动,滚动条隐藏保美观(滚轮/触摸仍可滚) */
.dash-scroll {
  overflow-y: auto;
  /* 4 条行高(约 4×40px)封顶,数据少时随内容收缩 */
  max-height: 16.4rem;
  /* firefox */
  scrollbar-width: none;
}

.dash-scroll::-webkit-scrollbar {
  display: none;
}

/* 集群池卡比明细行高,滚动上限放宽到约 2.5 张 */
.dash-scroll--tall {
  max-height: 24rem;
}

.dash-pool {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--c-line-strong);
  border-radius: 8px;
  font-size: 13px;
}

.dash-pool--link {
  cursor: pointer;
  transition: border-color 0.2s ease;
}

.dash-pool--link:hover {
  border-color: rgb(var(--n-primary-color));
}
</style>
