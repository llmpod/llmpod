<script setup lang="ts">
import '@/views/dashboard-shared/seg-group.css'
import { useController } from './useController'

const {
  loading,
  windowOptions,
  activeWindow,
  customRange,
  setWindow,
  setCustomRange,
  isDateDisabled,
  setFilter,
  filterModel,
  filterTenantId,
  filterApiKey,
  modelOptions,
  tenantOptions,
  apiKeyOptions,
  statCards,
  compareText,
  sourceText,
  trendEl,
  distCharts,
  setDistEl,
  DIST_PANELS,
  distributions,
  ModelTable,
} = useController()
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 页头:窗口 tabs + 整屏筛选 + 数据源徽标(§7.4) -->
    <LmCard>
      <div class="flex items-center justify-between flex-wrap gap-[.75rem]">
        <div class="flex items-center gap-[.75rem] flex-wrap min-w-0">
          <span class="stat-card-title shrink-0">调用统计</span>
          <n-radio-group :value="activeWindow" size="small" class="dash-seg-group" @update:value="setWindow">
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
        <div class="flex items-center gap-[.5rem] flex-wrap">
          <!-- 筛选显式绑定:变更即回骨架重拉,clearable 清空发 null/''(§6 屏2 #4) -->
          <LmSelect
            :value="filterModel"
            :options="modelOptions"
            clearable
            filterable
            placeholder="模型:全部"
            size="small"
            class="!w-[15rem]"
            @update:value="(value: string | null) => setFilter({ model: value ?? '' })"
          />
          <LmSelect
            :value="filterTenantId"
            :options="tenantOptions"
            clearable
            filterable
            placeholder="租户:全部"
            size="small"
            class="!w-[16rem]"
            @update:value="(value: number | null) => setFilter({ tenantId: value ?? null })"
          />
          <LmSelect
            :value="filterApiKey"
            :options="apiKeyOptions"
            clearable
            filterable
            placeholder="API Key:全部"
            size="small"
            class="!w-[15rem]"
            @update:value="(value: number | null) => setFilter({ apiKeyId: value ?? null })"
          />
          <!-- <n-tag v-if="!loading && sourceText" size="small" :bordered="false">{{ sourceText }}</n-tag> -->
        </div>
      </div>
      <!-- <div class="stat-filter-note">筛选作用于全屏指标(不含引擎网格);带筛选时数据走 5 分钟桶,在途请求数不可用</div> -->
    </LmCard>

    <!-- KPI 六卡(窗口级,面板标题不写死窗口,§6 屏2 #4) -->
    <div class="grid grid-cols-2 gap-[.75rem] md:grid-cols-3 xl:grid-cols-6">
      <template v-if="loading">
        <LmCard v-for="i in 6" :key="i">
          <n-skeleton text style="display: block; width: 4.8rem; font-size: 12px; margin: 1.2px 0" />
          <n-skeleton text style="display: block; width: 6rem; font-size: 20px; margin-top: 10px" />
          <n-skeleton text style="display: block; width: 5.4rem; font-size: 12px; margin-top: 6px" />
        </LmCard>
      </template>
      <template v-else>
        <n-empty
          v-if="!statCards.length"
          description="暂无统计数据"
          class="col-span-2 md:col-span-3 xl:col-span-6 py-[2rem]"
        />
        <LmCard v-for="card in statCards" :key="card.label">
          <div class="stat-subtitle">{{ card.label }}</div>
          <div class="stat-value">{{ card.value }}</div>
          <div
            v-if="card.deltaText"
            class="stat-delta"
            :class="card.deltaTone === 'bad' ? 'stat-delta--bad' : card.deltaTone === 'good' ? 'stat-delta--good' : ''"
          >
            {{ card.deltaText }}
          </div>
        </LmCard>
      </template>
    </div>

    <!-- 调用趋势 · 窗口内(全宽堆叠图:总量+构成同一张图,§6 屏2 #3) -->
    <LmCard>
      <div class="stat-card-header flex-wrap gap-y-[.4rem]">
        <span class="stat-card-title">调用趋势</span>
        <span class="stat-subtitle">{{ compareText ? `环比口径:${compareText}` : '当前窗口无环比' }}</span>
      </div>
      <n-skeleton v-if="loading" :sharp="false" style="width: 100%; height: 320px" />
      <div v-show="!loading" ref="trendEl" class="stat-chart-lg" />
    </LmCard>

    <!-- 四张分布 · 并排一行(累计桶已差分;看长尾,别只看均值,§6 屏2 #5) -->
    <div class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2 xl:grid-cols-4">
      <LmCard v-for="panel in DIST_PANELS" :key="panel.key">
        <div class="stat-card-header">
          <span class="stat-card-title text-small">{{ panel.title }}</span>
        </div>
        <template v-if="loading">
          <n-skeleton :sharp="false" style="width: 100%; height: 200px" />
        </template>
        <template v-else>
          <!-- 容器常驻:无样本时渲染空图表骨架(§6 屏2 #5)。ref 走函数式绑定 -->
          <div :ref="el => setDistEl(panel.key, el)" class="stat-chart-sm" />
          <div v-if="distributions[panel.key]?.avgText" class="stat-subtitle">
            平均 {{ distributions[panel.key]!.avgText }}
            <template v-if="distributions[panel.key]?.subText"> · {{ distributions[panel.key]!.subText }}</template>
          </div>
        </template>
      </LmCard>
    </div>

    <!-- 模型统计(宽表:调用量/占比/延迟/错误率/截断率/token 四列;末行合计) -->
    <LmCard>
      <div class="stat-card-header">
        <span class="stat-card-title">模型统计</span>
      </div>
      <ModelTable>
        <template #empty>
          <LmTableEmpty description="暂无模型调用数据" />
        </template>
      </ModelTable>
    </LmCard>
  </div>
</template>

<style scoped>
.stat-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--c-strong);
}

.stat-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

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

/* 环比行:中性=次色,good/bad 走语义色 */
.stat-delta {
  font-size: 12px;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--c-secondary);
}

.stat-delta--good {
  color: rgb(var(--n-success-color));
}

.stat-delta--bad {
  color: rgb(var(--n-error-color));
}

.stat-filter-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--c-secondary);
}

.stat-chart-lg {
  width: 100%;
  height: 320px;
}

.stat-chart-sm {
  width: 100%;
  height: 200px;
}

/* 合计行按 rowClassName 标记加粗(不依赖行序,规避与其他行重名碰撞) */
:deep(.stat-row--total) {
  font-weight: 600;
}
</style>
