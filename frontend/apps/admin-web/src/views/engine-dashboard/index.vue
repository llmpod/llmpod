<script setup lang="ts">
import '@/views/dashboard-shared/seg-group.css'
import { useController } from './useController'

const {
  realtimeLoading,
  historyLoading,
  anomalyLoading,
  realtimeWindow,
  realtimeWindowLabel,
  customRange,
  windowOptions,
  setRealtimeWindow,
  setCustomRange,
  isDateDisabled,
  indexed,
  canIndex,
  toggleIndexed,
  banner,
  kpiCards,
  engineRows,
  throughputEl,
  heatmapEl,
  anomalyView,
  AnomalyTable,
  UpstreamTable,
  hasUpstreamData,
  hasUpstreamProviders,
  tsText,
  KV_SATURATION_RATE,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 关注横幅(前端自算;§8 #9) -->
    <LmCard v-if="!banner.ok && !realtimeLoading">
      <div class="flex items-center gap-[.6rem] flex-wrap">
        <span class="text-base font-semibold eng-text--warn">▲ {{ banner.items.length }} 项需关注</span>
        <span class="eng-subtitle truncate">{{ banner.items.join(' · ') }}</span>
      </div>
    </LmCard>

    <!-- 页头:/realtime 窗口 tabs(独立状态;§6 屏3 #6) -->
    <LmCard>
      <div class="flex items-center justify-between flex-wrap gap-[.75rem]">
        <div class="flex items-center gap-[.75rem] flex-wrap min-w-0">
          <span class="eng-card-title shrink-0">推理引擎</span>
          <n-radio-group :value="realtimeWindow" size="small" class="dash-seg-group" @update:value="setRealtimeWindow">
            <n-radio-button
              v-for="option in windowOptions"
              :key="option.value"
              :value="option.value"
              :label="option.label"
            />
          </n-radio-group>
          <!-- 常驻区间展示:输入框只显日期,面板内可调时间;快捷窗口显示当前实际区间,确认后切自定义重拉(≤90 天) -->
          <n-date-picker
            :value="customRange"
            type="datetimerange"
            format="yyyy-MM-dd"
            size="small"
            class="!w-[25rem]"
            :is-date-disabled="isDateDisabled"
            @update:value="(value: [number, number] | null) => setCustomRange(value)"
          />
        </div>
        <!-- <span class="eng-subtitle shrink-0">{{ tsText || '—' }}</span> -->
      </div>
    </LmCard>

    <!-- KPI 八卡:前 4 张「还吃得下吗」、后 4 张「有没有在坏」(§7.5) -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-4 xl:grid-cols-8">
      <template v-if="realtimeLoading">
        <LmCard v-for="i in 8" :key="i">
          <n-skeleton text style="display: block; width: 4.8rem; font-size: 12px; margin: 1.2px 0" />
          <n-skeleton text style="display: block; width: 4.8rem; font-size: 18px; margin-top: 8px" />
          <n-skeleton text style="display: block; width: 5.4rem; font-size: 12px; margin-top: 6px" />
        </LmCard>
      </template>
      <template v-else>
        <n-empty
          v-if="!kpiCards.length"
          description="暂无引擎指标"
          class="col-span-2 md:col-span-4 xl:col-span-8 py-[2rem]"
        />
        <LmCard v-for="card in kpiCards" :key="card.label">
          <div class="eng-subtitle">{{ card.label }}</div>
          <div class="eng-stat-value">{{ card.value }}</div>
          <div
            v-if="card.deltaText"
            class="eng-delta"
            :class="card.deltaTone === 'bad' ? 'eng-delta--bad' : card.deltaTone === 'good' ? 'eng-delta--good' : ''"
          >
            {{ card.deltaText }}
          </div>
          <div v-if="card.note" class="eng-subtitle mt-[.2rem]">{{ card.note }}</div>
        </LmCard>
      </template>
    </div>

    <!-- 吞吐趋势(索引化切换)+ KV 热力图(共用页头窗口) -->
    <div class="grid grid-cols-1 gap-[.75rem] lg:grid-cols-2">
      <LmCard>
        <div class="eng-card-header flex-wrap gap-y-[.4rem]">
          <span class="eng-card-title">吞吐趋势</span>
          <div v-if="canIndex" class="flex items-center gap-[.5rem]">
            <span class="eng-subtitle">{{ indexed ? '基准化(首槽=100)' : '绝对量' }}</span>
            <n-switch size="small" :value="indexed" @update:value="toggleIndexed" />
          </div>
        </div>
        <n-skeleton v-if="realtimeLoading" :sharp="false" style="width: 100%; height: 300px" />
        <div v-show="!realtimeLoading" ref="throughputEl" class="eng-chart" />
      </LmCard>

      <LmCard>
        <div class="eng-card-header flex-wrap gap-y-[.4rem]">
          <span class="eng-card-title">KV 压力</span>
          <!-- <span class="eng-subtitle">缺档 = 未抓到(非水位 0)</span> -->
        </div>
        <n-skeleton v-if="historyLoading" :sharp="false" style="width: 100%; height: 300px" />
        <div v-show="!historyLoading" ref="heatmapEl" class="eng-chart" />
        <div class="eng-heat-legend">
          <span><i class="eng-heat-chip" style="background:#6fbf8b" />&lt;40%</span>
          <span><i class="eng-heat-chip" style="background:#f2d24c" />40-70%</span>
          <span><i class="eng-heat-chip" style="background:#ef8f4e" />70-85%</span>
          <span><i class="eng-heat-chip" style="background:#e05252" />&gt;85%</span>
          <span class="eng-heat-chip eng-heat-chip--warn" />发生抢占
        </div>
      </LmCard>
    </div>

    <!-- 引擎实例状态(实时网格;§3.3 ok=false → 指标不可用) -->
    <LmCard>
      <div class="eng-card-header">
        <span class="eng-card-title">引擎实例状态</span>
        <!-- <span class="eng-subtitle">刷新 10s</span> -->
      </div>
      <template v-if="realtimeLoading">
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-[.75rem]">
          <n-skeleton v-for="i in 4" :key="i" :sharp="false" style="height: 9.6rem" />
        </div>
      </template>
      <template v-else>
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-[.75rem]">
          <div v-for="row in engineRows" :key="row.id" class="eng-card" :class="{ 'eng-card--down': !row.ok }">
            <div class="flex items-center justify-between gap-[.5rem]">
              <span class="truncate text-small">
                {{ row.name }}
                <span class="eng-subtitle">#{{ row.rank }}</span>
              </span>
              <n-tag size="small" :bordered="false" :type="row.ok ? 'success' : 'warning'">
                {{ row.ok ? row.status : '指标不可用' }}
              </n-tag>
            </div>
            <template v-if="row.ok">
              <div class="flex items-center gap-[.5rem]">
                <n-progress
                  type="line"
                  class="flex-1"
                  :percentage="Math.round((row.kvPct ?? 0) * 100)"
                  :show-indicator="false"
                  :height="6"
                  :border-radius="3"
                  :color="row.kvPct !== null && row.kvPct > KV_SATURATION_RATE ? 'rgb(var(--n-warning-color))' : 'rgb(var(--n-primary-color))'"
                />
                <span class="text-small tabular-nums w-[3rem] text-right">{{ row.kvText }}</span>
              </div>
              <div class="grid grid-cols-4 gap-[.25rem] text-center">
                <div>
                  <div class="eng-metric">{{ row.waiting }}</div>
                  <div class="eng-subtitle">排队</div>
                </div>
                <div>
                  <div class="eng-metric">{{ row.running }}</div>
                  <div class="eng-subtitle">running</div>
                </div>
                <div>
                  <div class="eng-metric">{{ row.hitText }}</div>
                  <div class="eng-subtitle">命中</div>
                </div>
                <div>
                  <div class="eng-metric" :class="row.preemptionAlert ? 'eng-text--warn' : ''">
                    {{ row.preemptions }}
                  </div>
                  <div class="eng-subtitle">抢占</div>
                </div>
              </div>
              <div class="eng-subtitle">数据 {{ row.ageText }}</div>
            </template>
            <div v-else class="eng-subtitle">该实例未暴露 Prometheus 指标或抓取失败,指标不可信</div>
          </div>
          <n-empty v-if="!engineRows.length" description="暂无活跃实例" class="col-span-full py-[2rem]" />
        </div>
      </template>
    </LmCard>

    <!-- 上游健康 + 异常信息(共用页头窗口;§7.5 底行) -->
    <div class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2">
      <LmCard>
        <div class="eng-card-header">
          <span class="eng-card-title">上游健康</span>
          <!-- <span class="eng-subtitle">窗口 {{ realtimeWindowLabel }}</span> -->
        </div>
        <template v-if="anomalyLoading">
          <n-skeleton v-for="i in 4" :key="i" text style="display: block; font-size: 13px; margin: 9px 0" />
        </template>
        <template v-else>
          <UpstreamTable>
            <template #empty>
              <LmTableEmpty description="暂无上游健康数据" />
            </template>
          </UpstreamTable>
        </template>
      </LmCard>

      <LmCard>
        <div class="eng-card-header">
          <span class="eng-card-title">异常信息</span>
          <div v-if="anomalyView?.items.length" class="flex items-center gap-[.75rem] flex-wrap">
            <span class="text-small">合计 <b class="tabular-nums">{{ anomalyView.totalText }}</b> 次</span>
            <span class="text-small">占总请求 {{ anomalyView.ratioText }}</span>
          </div>
        </div>
        <template v-if="anomalyLoading">
          <n-skeleton v-for="i in 5" :key="i" text style="display: block; font-size: 13px; margin: 9px 0" />
        </template>
        <template v-else>
          <div class="eng-table-plain">
            <AnomalyTable>
              <template #empty>
                <LmTableEmpty :description="anomalyView ? '窗口内无异常' : '暂无异常数据'" />
              </template>
            </AnomalyTable>
          </div>
        </template>
      </LmCard>
    </div>
  </div>
</template>

<style scoped>
.eng-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--c-strong);
}

.eng-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.eng-subtitle {
  font-size: 12px;
  color: var(--c-secondary);
}

.eng-stat-value {
  font-size: 18px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-strong);
}

.eng-delta {
  font-size: 12px;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-secondary);
}

.eng-delta--good {
  color: rgb(var(--n-success-color));
}

.eng-delta--bad {
  color: rgb(var(--n-error-color));
}

.eng-text--warn {
  color: rgb(var(--n-warning-color));
}

.eng-chart {
  width: 100%;
  height: 300px;
}

/* 引擎实例卡片 */
.eng-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--c-line-strong);
  border-radius: 8px;
}

.eng-card--down {
  border-style: dashed;
}

.eng-metric {
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* 热力图图例 */
.eng-heat-legend {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--c-secondary);
}

.eng-heat-chip {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: -1px;
}

.eng-heat-chip--warn {
  border: 1.5px solid rgb(var(--n-warning-color));
  background: transparent;
  margin-right: 4px;
}

/* 表格行间不要间隔线(含末行收口线,表头下沿保留)。
   LmTable 未暴露该开关,naive 的 `--bottom-bordered` 末行规则特异性更高,需 !important 穿透 */
.eng-table-plain :deep(.n-data-table-td) {
  border-bottom: none !important;
}
</style>
