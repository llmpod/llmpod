<script setup lang="ts">
import { useController } from './useController'

const {
  loading,
  greeting,
  username,
  quickActions,
  goRoute,
  statCards,
  trendChartEl,
  myModels,
  recentCalls,
  wallet,
  TimeOutline,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 问候 + 快捷操作 -->
    <LmCard>
      <div v-if="loading" class="console-header">
        <div class="console-greeting-wrap">
          <n-skeleton text style="width: 16rem; font-size: 18px; margin: 2.7px 0" />
          <n-skeleton text style="width: 13rem; font-size: 12px; margin-top: 9.6px" />
        </div>
        <div class="hidden w-[.1rem] h-[6rem] border-l mr-[.8rem] lg:block"></div>
        <div class="console-quick">
          <n-skeleton text style="width: 4rem; font-size: 14px; margin: 1.4px 0" />
          <div class="console-quick-actions">
            <n-skeleton
              v-for="i in 5"
              :key="i"
              :sharp="false"
              style="width: 6.8rem; height: 31px"
            />
          </div>
        </div>
      </div>
      <div v-else class="console-header">
        <div class="console-greeting-wrap">
          <div class="console-greeting">{{ greeting }},{{ username }}</div>
          <div class="console-subtitle">数据窗口: 今日 00:00 至当前</div>
        </div>
        <div class="hidden w-[.1rem] h-[6rem] border-l mr-[.8rem] lg:block"></div>
        <div class="console-quick">
          <span class="console-quick-title">快捷操作</span>
          <div class="console-quick-actions">
            <div
              v-for="action in quickActions"
              :key="action.label"
              class="console-quick-item"
              @click="goRoute(action.name)"
            >
              <n-icon size="16">
                <component :is="action.icon" />
              </n-icon>
              <span>{{ action.label }}</span>
            </div>
          </div>
        </div>
      </div>
    </LmCard>

    <!-- 统计卡片:窄屏 2 列起步,≥md 恢复 4 列 -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-4">
      <template v-if="loading">
        <LmCard v-for="i in 4" :key="i">
          <n-skeleton text style="display: block; width: 4.8rem; font-size: 12px; margin: 1.2px 0" />
          <n-skeleton
            text
            style="display: block; width: 6rem; font-size: 20px; margin-top: 6.4px"
          />
          <n-skeleton
            text
            style="display: block; width: 8rem; font-size: 12px; margin-top: 9.6px"
          />
        </LmCard>
      </template>
      <template v-else>
        <LmCard v-for="card in statCards" :key="card.label">
          <div class="stat-subtitle">
            {{ card.label }}
          </div>
          <div class="stat-value">
            {{ card.value }}
          </div>
        </LmCard>
      </template>
    </div>

    <!-- 调用趋势 + 账户信息:窄屏纵向堆叠,≥md 恢复 2:1 双列 -->
    <div class="grid grid-cols-1 gap-[.75rem] md:grid-cols-3">
      <LmCard class="col-span-1 md:col-span-2">
        <div class="stat-card-header">
          <n-skeleton v-if="loading" text style="width: 7.5rem; font-size: 15px; margin: 4.5px 0" />
          <span v-else class="stat-card-title">近7日调用趋势</span>
        </div>
        <n-skeleton v-if="loading" :sharp="false" style="width: 100%; height: 300px" />
        <div v-show="!loading" ref="trendChartEl" class="stat-chart" />
      </LmCard>

      <div class="flex flex-col gap-[.75rem]">
        <template v-if="loading">
          <LmCard v-for="i in 3" :key="i" class="flex-1">
            <n-skeleton
              text
              style="display: block; width: 4.8rem; font-size: 12px; margin: 1.2px 0"
            />
            <n-skeleton
              text
              style="display: block; width: 6rem; font-size: 20px; margin-top: 6.4px"
            />
            <n-skeleton
              :sharp="false"
              style="display: block; width: 100%; height: 14px; margin-top: 14px"
            />
          </LmCard>
        </template>
        <template v-else>
          <LmCard class="flex-1">
            <div class="stat-subtitle">账户余额</div>
            <div class="stat-value">¥{{ wallet?.balance ?? '—' }}</div>
            <div class="console-card-footer stat-subtitle">
              在途冻结 ¥{{ wallet?.frozen ?? '—' }}
            </div>
          </LmCard>

          <LmCard class="flex-1">
            <div class="stat-subtitle">可用余额</div>
            <div class="stat-value">¥{{ wallet?.available ?? '—' }}</div>
            <div class="console-balance">
              <n-progress
                type="line"
                :percentage="wallet?.availablePercent ?? 0"
                :show-indicator="false"
                :height="6"
                border-radius="3px"
              />
              <span class="stat-subtitle console-balance-text">可用占比{{ wallet?.availablePercent ?? 0 }}%</span>
            </div>
          </LmCard>

          <LmCard class="flex-1">
            <div class="stat-subtitle">本月消费</div>
            <div class="stat-value">¥{{ wallet?.monthConsume ?? '—' }}</div>
            <div class="console-card-footer">
              <router-link class="console-link" :to="{ name: 'cost' }"> 查看详情&gt; </router-link>
            </div>
          </LmCard>
        </template>
      </div>
    </div>

    <!-- 我的模型 + 最近调用:窄屏纵向堆叠,≥xl 恢复双列 -->
    <div class="grid grid-cols-1 gap-[.75rem] xl:grid-cols-2">
      <template v-if="loading">
        <LmCard v-for="card in 2" :key="card">
          <div class="stat-card-header">
            <n-skeleton text style="width: 4.5rem; font-size: 15px; margin: 4.5px 0" />
          </div>
          <div v-for="i in 5" :key="i" class="console-list-item">
            <div class="console-list-main flex flex-col gap-[.4rem]">
              <n-skeleton text style="width: 10rem; font-size: 13px" />
              <n-skeleton text style="width: 16rem; font-size: 12px" />
            </div>
            <n-skeleton :sharp="false" style="width: 4.6rem; height: 22px" />
          </div>
        </LmCard>
      </template>
      <template v-else>
        <LmCard class="console-list-card">
          <div class="stat-card-header">
            <span class="stat-card-title">我的模型</span>
          </div>
          <div
            v-for="model in myModels"
            :key="model.id"
            class="console-list-item"
          >
            <div class="console-list-main">
              <div class="console-list-title truncate">
                {{ model.name }}
              </div>
              <div class="stat-subtitle truncate">
                模型ID {{ model.id }}
              </div>
            </div>
            <n-tag size="small" :bordered="false" type="success">
              可用
            </n-tag>
          </div>
          <n-empty v-if="!myModels.length" size="small" description="暂无可用模型" class="my-auto py-[1rem]" />
        </LmCard>

        <LmCard class="console-list-card">
          <div class="stat-card-header">
            <span class="stat-card-title">最近调用</span>
          </div>
          <div
            v-for="(call, index) in recentCalls"
            :key="index"
            class="console-list-item"
          >
            <div class="console-list-main">
              <div class="console-call-time">
                <n-icon size="14">
                  <TimeOutline />
                </n-icon>
                <span class="console-list-title">{{ call.time }}</span>
              </div>
              <div class="stat-subtitle">In: {{ call.input }}&nbsp;&nbsp;Out: {{ call.output }}</div>
            </div>
            <div class="console-call-side">
              <n-tag size="small" :bordered="false" type="primary">
                {{ call.model }}
              </n-tag>
              <span
                class="console-list-title console-call-latency"
                :class="{
                  'stat-text--error': call.tone === 'error',
                  'stat-text--warning': call.tone === 'warning',
                }"
              >{{ call.latency }}</span>
            </div>
          </div>
          <n-empty v-if="!recentCalls.length" size="small" description="暂无调用记录" class="my-auto py-[1rem]" />
        </LmCard>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 问候 + 快捷操作头:窄屏纵向堆叠(分隔线隐藏),≥lg 恢复左右分栏 */
.console-header {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.console-greeting-wrap {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
}

@media (min-width: 1024px) {
  .console-header {
    flex-direction: row;
    align-items: center;
  }

  .console-greeting-wrap {
    width: 32.5rem;
  }
}

.console-greeting {
  font-size: 18px;
  font-weight: 600;
  color: var(--c-strong);
}

.console-subtitle {
  font-size: 12px;
  color: var(--c-secondary);
  margin-top: 6px;
}

/* 快捷操作 */
.console-quick {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.console-quick-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--c-strong);
}

.console-quick-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.console-quick-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  color: var(--c-strong);
  /* n-layout 内 --n-text-color 是 hex,无法用 rgba() 调透明度,改用 color-mix */
  background-color: color-mix(in srgb, var(--c-strong) 5%, transparent);
  transition: background-color 0.2s;
}

.console-quick-item:hover {
  background-color: color-mix(in srgb, var(--c-strong) 10%, transparent);
}

/* 统计卡片 */
.stat-subtitle {
  font-size: 12px;
  color: var(--c-secondary);
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

.stat-delta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
}

.stat-text--success {
  color: rgb(var(--n-success-color));
}

.stat-text--error {
  color: rgb(var(--n-error-color));
}

.stat-text--warning {
  color: rgb(var(--n-warning-color));
}

.stat-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--c-strong);
}

.stat-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.stat-chart {
  width: 100%;
  height: 300px;
}

/* 账户余额进度条 */
.console-balance {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.console-balance :deep(.n-progress) {
  flex: 1;
}

.console-balance-text {
  white-space: nowrap;
}

.console-card-footer {
  margin-top: 8px;
}

.console-link {
  font-size: 12px;
  color: rgb(var(--n-primary-color));
  text-decoration: none;
}

/* 列表卡片内容区变 flex 列:空态时 n-empty 用 my-auto 吸收剩余高度,垂直居中于卡片中央 */
.console-list-card :deep(.n-card-content) {
  display: flex;
  flex-direction: column;
}

/* 列表项 */
.console-list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--c-line);
}

.console-list-item:last-child {
  border-bottom: none;
}

.console-list-main {
  min-width: 0;
}

.console-list-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--c-strong);
}

.console-call-time {
  display: flex;
  align-items: center;
  gap: 6px;
}

.console-call-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
}

.console-call-latency {
  font-variant-numeric: tabular-nums;
}
</style>
