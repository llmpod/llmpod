<script setup lang="ts">
import { HardwareChipOutline } from '@vicons/ionicons5'
import { gpuBrandIcon } from '@/views/node-pool/topology/components/brandIcons'
import {
  formatGbPair,
  formatPercent,
  gpuStatusMap,
  progressColor,
  type GpuData,
  type GpuRuntime,
} from './useStore'

/**
 * 表格行展开内容:GPU 清单子表(细边框圆角面板 + 表头 + 数据行)。
 * 面板让展开区与父行形成从属层级;列义由表头一次交代,数据行不再重复灰色标签,
 * 多卡时同列纵向对齐、一眼可扫;数据直接取列表行聚合的 gpus,展开不再单独发请求;
 * 驱动版本/GPU 数量等汇总信息主表格行已有,此处不重复展示。
 * 型号列带厂商品牌图标(gpuBrandIcon 按型号匹配 NVIDIA/昇腾,未命中回退通用芯片图标)。
 * 显存/利用率列均为 文字在上 + 迷你进度条在下 的纵向结构,与主表格用量单元格一致:
 * 显存列展示 已用/总量 + 占用进度条;利用率列展示核心利用率 + 进度条(悬浮交代显存利用率/温度摘要);
 * runtime 为 null(节点离线或心跳未上报)时利用率渲染 —、显存列回退静态总量,不把缺数据当 0,
 * 与主表格「GPU 负载」列的单卡明细形成呼应。
 */
const props = defineProps<{ gpus: GpuData[] }>()

/** 单卡厂商品牌图标或通用芯片图标 */
function iconOf(g: GpuData) {
  return gpuBrandIcon(g.name) ?? HardwareChipOutline
}

/** 利用率列悬浮摘要:核心/显存利用率 + 温度 */
function utilTip(rt: GpuRuntime): string {
  return `核心 ${formatPercent(rt.utilization)} · 显存 ${formatPercent(rt.memory_utilization)} · ${rt.temperature}℃`
}

/** 进度条宽度(%),clamp 到 0-100 */
function barWidth(percent: number): string {
  return `${Math.min(100, Math.max(0, percent))}%`
}

/** 显存占用百分比:已用/总量,总量为 0 时防御为 0 */
function vramPercent(rt: GpuRuntime, total: number): number {
  return total > 0 ? (rt.used_vram_gb / total) * 100 : 0
}
</script>

<template>
  <div
    v-if="gpus.length"
    class="rounded-md border border-line-strong bg-fill-2 px-[1.2rem] text-base"
  >
    <!-- 表头:一次交代列义,与主表格的表头+行视觉语言一致 -->
    <div class="gpu-grid border-b border-line py-[.6rem] text-xs text-secondary">
      <span>GPU</span>
      <span>型号</span>
      <span>状态</span>
      <span>架构</span>
      <span>显存</span>
      <span>负载</span>
      <span>驱动</span>
      <span>UUID</span>
    </div>
    <div
      v-for="g in gpus"
      :key="g.id"
      class="gpu-grid border-b border-line py-[.7rem] last:border-b-0"
    >
      <!-- 身份:卡序号 -->
      <span class="tabular-nums whitespace-nowrap">{{ g.index }}</span>
      <!-- 型号:品牌图标 + 型号文字 -->
      <span class="flex min-w-0 items-center gap-[.6rem]">
        <n-icon :size="16" class="shrink-0 text-secondary">
          <component :is="iconOf(g)" />
        </n-icon>
        <span class="min-w-0 truncate font-medium" :title="g.name">{{ g.name }}</span>
      </span>
      <!-- 状态 -->
      <span>
        <n-tag
          size="small"
          :type="(gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).type"
          :bordered="false"
        >
          {{ (gpuStatusMap[g.status] ?? { label: g.status, type: 'default' }).label }}
        </n-tag>
      </span>
      <!-- 规格:架构 / 驱动 / UUID,同列纵向对齐、超长截断 -->
      <span class="truncate" :title="g.arch">{{ g.arch }}</span>
      <!-- 显存:有实时数据展示 已用/总量 + 占用进度条(与主表格用量列结构一致),离线回退静态总量 -->
      <span
        v-if="g.runtime"
        class="flex flex-col gap-[.3rem] whitespace-nowrap"
        :title="`已用 ${formatGbPair(g.runtime.used_vram_gb, g.total_vram_gb)}`"
      >
        <span class="tabular-nums truncate">{{ formatGbPair(g.runtime.used_vram_gb, g.total_vram_gb) }}</span>
        <span class="h-[.4rem] w-[6rem] overflow-hidden rounded-full bg-fill-4">
          <span
            class="block h-full rounded-full"
            :class="progressColor(vramPercent(g.runtime, g.total_vram_gb))"
            :style="{ width: barWidth(vramPercent(g.runtime, g.total_vram_gb)) }"
          />
        </span>
      </span>
      <span v-else class="tabular-nums whitespace-nowrap">{{ g.total_vram_gb }}GB</span>
      <!-- 利用率:核心利用率 + 迷你进度条,文字在上/条在下与主表格用量列结构一致,悬浮交代显存利用率/温度;离线渲染 — -->
      <span
        v-if="g.runtime"
        class="flex flex-col gap-[.3rem] whitespace-nowrap"
        :title="utilTip(g.runtime)"
      >
        <span class="tabular-nums font-medium">{{ formatPercent(g.runtime.utilization) }}</span>
        <span class="h-[.4rem] w-[6rem] overflow-hidden rounded-full bg-fill-4">
          <span
            class="block h-full rounded-full"
            :class="progressColor(g.runtime.utilization)"
            :style="{ width: barWidth(g.runtime.utilization) }"
          />
        </span>
      </span>
      <span v-else class="text-secondary">—</span>
      <span class="truncate" :class="g.driver_version ? '' : 'text-error'">{{
        g.driver_version ?? '未安装'
      }}</span>
      <span class="min-w-0 truncate font-mono text-xs text-secondary" :title="g.uuid">{{
        g.uuid
      }}</span>
    </div>
  </div>
  <n-empty v-else description="该节点暂无 GPU" class="py-[2rem]" />
</template>

<style scoped lang="scss">
/*
 * 八列栅格:GPU | 型号 | 状态 | 架构 | 显存 | 利用率 | 驱动 | UUID;表头与数据行共用,同列纵向对齐。
 * 关键:每行是独立 grid 容器,窄列必须用固定宽度——若用 max-content 会随各行内容(如"状态"文字 vs n-tag)
 * 解析出不同列宽,导致表头与数据行错位;fr 列在同一面板宽度下解析结果恒一致,不受内容影响。
 */
.gpu-grid {
  display: grid;
  align-items: center;
  grid-template-columns:
    2.6rem minmax(0, 1.5fr) 7rem minmax(0, 0.8fr) 9.5rem
    8rem minmax(0, 1fr) minmax(0, 1.5fr);
  column-gap: 1.6rem;
}
</style>
