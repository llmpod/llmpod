<script setup lang="ts">
import type { Component } from 'vue'
import {
  AppsOutline,
  BarChartOutline,
  CardOutline,
  DocumentTextOutline,
  InformationCircleOutline,
  KeyOutline,
  PeopleOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
  SpeedometerOutline,
  StorefrontOutline,
  TerminalOutline,
} from '@vicons/ionicons5'
import { useMenuVisibility, type MenuRow } from '../useMenuVisibility'

const { loading, savingPath, rows, load, toggle } = useMenuVisibility()

// 进入 tab 拉取本人开关态(直读不走缓存,表单即最新)
onMounted(() => void load())

/** 模块卡片图标:按路由 name 对齐左侧菜单各模块的心智;未声明的路径用通用图标兜底 */
const MODULE_ICONS: Record<string, Component> = {
  console: SpeedometerOutline,
  'model-mark': StorefrontOutline,
  playground: TerminalOutline,
  'api-key': KeyOutline,
  statistics: BarChartOutline,
  cost: CardOutline,
  team: PeopleOutline,
  logs: DocumentTextOutline,
  auth: ShieldCheckmarkOutline,
  config: SettingsOutline,
  about: InformationCircleOutline,
}

/** 路由 name → 模块图标 */
function iconOf(name: string): Component {
  return MODULE_ICONS[name] ?? AppsOutline
}

/** 行按侧栏分组连续分桶(声明序即展示序):分区与左侧菜单心智一致,空组自然不出现 */
const groups = computed(() =>
  rows.value.reduce<Array<{ label: string; icon?: Component; items: MenuRow[] }>>((buckets, row) => {
    const last = buckets[buckets.length - 1]
    if (last?.label === row.group) last.items.push(row)
    else buckets.push({ label: row.group, icon: row.groupIcon, items: [row] })
    return buckets
  }, []),
)
</script>

<template>
  <LmCard
    title="模块开关"
    segmented
  >
    <div class="flex flex-col gap-[1.2rem] pt-[.5rem]">
      <!-- min-h 定面板高度基线:tab 切入时 rows 尚未返回,若随空态塌陷,面板高度过渡会
           先收缩再被数据撑开(两段跳动);取略高于加载完成态,spinner 在此范围内居中 -->
      <n-spin
        :show="loading"
        class="min-h-[48rem]"
      >
        <div
          v-if="rows.length"
          class="flex flex-col gap-[1.6rem]"
        >
          <!-- 分区标题与左侧菜单分组同源:按区扫视,组内仍是卡片网格 -->
          <div
            v-for="group in groups"
            :key="group.label"
            class="flex flex-col gap-[1rem]"
          >
            <!-- 分区图标复用侧栏分组图标(svg mask,currentColor 跟随文字色) -->
            <div class="flex items-center gap-[.8rem]">
              <template v-if="group.icon">
                <component
                  :is="group.icon"
                  class="shrink-0 text-primary"
                />
              </template>
              <span class="text-base font-medium text-default">{{ group.label }}</span>
            </div>
            <div class="grid grid-cols-[repeat(auto-fill,minmax(22rem,1fr))] gap-[1rem]">
              <!-- 模块卡片:显示=实线边框+浅底;本人关闭=虚线边框+幽灵化,状态即层级 -->
              <div
                v-for="item in group.items"
                :key="item.path"
                class="flex flex-col gap-[.8rem] rounded-lg border p-[1.2rem] transition-colors duration-150 ease-out"
                :class="
                  item.enabled ? 'border-line-strong bg-fill-2' : 'border-dashed border-line-strong bg-transparent'
                "
              >
                <div class="flex min-w-0 items-center gap-[.8rem]">
                  <div
                    class="flex h-[2.8rem] w-[2.8rem] shrink-0 items-center justify-center rounded-[.8rem] transition-colors duration-150 ease-out"
                    :class="item.enabled ? 'bg-primary-soft text-primary' : 'bg-fill-4 text-icon'"
                  >
                    <n-icon :size="18">
                      <component :is="iconOf(item.name)" />
                    </n-icon>
                  </div>
                  <div class="flex min-w-0 flex-1 flex-col gap-[.2rem]">
                    <span
                      class="text-base truncate"
                      :class="item.enabled ? 'text-strong' : 'text-secondary'"
                    >
                      {{ item.label }}
                    </span>
                    <span class="text-xs text-secondary truncate">{{ group.label }}</span>
                  </div>
                  <!-- 锁定行(设置页)不常驻提示文字,悬浮开关时经 tooltip 说明 -->
                  <n-tooltip
                    :disabled="!item.locked"
                    trigger="hover"
                  >
                    <template #trigger>
                      <n-switch
                        size="small"
                        :loading="savingPath === item.path"
                        :disabled="item.locked"
                        :value="item.enabled"
                        @update:value="toggle(item.path, $event)"
                      />
                    </template>
                    设置入口,不可关闭
                  </n-tooltip>
                </div>
              </div>
            </div>
          </div>
        </div>
        <n-empty
          v-else
          class="py-[2rem]"
          description="暂无可配置的菜单"
        />
      </n-spin>
    </div>
  </LmCard>
</template>
