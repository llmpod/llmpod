<script setup lang="ts">
import { useController } from './useController'
import ModelDetailDrawer from './ModelDetailDrawer.vue'
import { brandIconOf, formatDecimal, modelTypeMap } from './useStore'

const {
  keyword,
  loading,
  filtered,
  openDetail,
  goPlayground,
  activeModel,
  detailDrawerShow,
} = useController()

/** 定价展示:null=不计费 */
function priceText(value: string | null): string {
  return value == null ? '不计费' : `¥${formatDecimal(value)}`
}
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 搜索 -->
    <LmCard content-class="flex flex-wrap items-center justify-between gap-[.75rem]">
      <div class="text-secondary text-base">
        以下为本租户可调用的已发布模型全集
      </div>
      <LmTableSearch
        v-model:model-value="keyword"
        :placeholder="'搜索模型名称'"
        :clearable="true"
        class="!w-[28rem]"
      />
    </LmCard>

    <!-- 骨架屏:模型卡片占位 -->
    <div
      v-if="loading"
      class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
    >
      <LmCard
        v-for="i in 6"
        :key="i"
      >
        <div class="flex flex-col gap-[1rem]">
          <div class="flex items-center justify-between gap-[1rem]">
            <n-skeleton
              text
              style="width: 12rem; font-size: 14px; margin: 2.8px 0"
            />
            <n-skeleton
              :sharp="false"
              style="width: 62px; height: 22px"
            />
          </div>
          <n-skeleton
            text
            style="width: 40%; font-size: 12px; margin: 1.2px 0"
          />
          <div class="flex items-end justify-between gap-[1rem]">
            <n-skeleton
              text
              style="width: 12rem; font-size: 12px; margin: 2px 0"
            />
            <n-skeleton
              :sharp="false"
              style="width: 76px; height: 32px"
            />
          </div>
        </div>
      </LmCard>
    </div>

    <!-- 模型卡片列表 -->
    <div
      v-else-if="filtered.length"
      class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
    >
      <LmCard
        v-for="model in filtered"
        :key="model.id"
        hoverable
        class="cursor-pointer"
        @click="openDetail(model)"
      >
        <div class="flex h-full flex-col gap-[1rem]">
          <!-- 品牌图标 + 模型名 + 类型/授权标签 -->
          <div class="flex items-center justify-between gap-[1rem]">
            <div class="flex min-w-0 items-center gap-[.75rem]">
              <div class="flex h-[2.6rem] w-[2.6rem] shrink-0 items-center justify-center rounded-lg bg-fill-2">
                <n-icon
                  :size="24"
                  class="text-tertiary"
                >
                  <component :is="brandIconOf(model.name)" />
                </n-icon>
              </div>
              <span class="truncate text-small font-semibold text-strong font-mono">
                {{ model.name }}
              </span>
            </div>
            <div class="flex shrink-0 items-center gap-[.25rem]">
              <n-tag
                size="small"
                :bordered="false"
              >
                {{ modelTypeMap[model.model_type] ?? model.model_type }}
              </n-tag>
              <n-tag
                size="small"
                type="success"
                :bordered="false"
              >
                可用
              </n-tag>
            </div>
          </div>
          <!-- 模型描述(一句话简介) -->
          <div
            class="text-small text-secondary truncate"
            :title="model.description || undefined"
          >
            {{ model.description || '暂无描述' }}
          </div>
          <!-- 能力标签(受控词表,纯展示) -->
          <div
            v-if="model.tags.length"
            class="flex flex-wrap gap-[.25rem]"
          >
            <n-tag
              v-for="tag in model.tags"
              :key="tag"
              size="small"
              type="primary"
              :bordered="false"
            >
              {{ tag }}
            </n-tag>
          </div>
          <!-- 定价摘要(左) + 在线调试按钮(右下角) -->
          <div class="mt-auto flex items-end justify-between gap-[1rem]">
            <div class="flex min-w-0 flex-col gap-[.5rem]">
              <div class="flex items-center gap-[.5rem] text-base">
                <span class="text-secondary">输入:</span>
                <span class="text-default">{{ priceText(model.input_price_per_mtok) }}</span>
                <span
                  v-if="model.input_price_per_mtok != null"
                  class="text-secondary text-base"
                >/ 百万 tokens</span>
              </div>
              <div class="flex items-center gap-[.5rem] text-base">
                <span class="text-secondary">输出:</span>
                <span class="text-default">{{ priceText(model.output_price_per_mtok) }}</span>
                <span
                  v-if="model.output_price_per_mtok != null"
                  class="text-secondary text-base"
                >/ 百万 tokens</span>
              </div>
            </div>
            <n-button
              type="primary"
              class="shrink-0"
              @click.stop="goPlayground(model)"
            >
              在线调试
            </n-button>
          </div>
        </div>
      </LmCard>
    </div>

    <!-- 空态 -->
    <LmCard v-else>
      <n-empty
        description="未找到匹配的模型"
        class="py-[2.5rem]"
      />
    </LmCard>

    <!-- 模型详情抽屉 -->
    <ModelDetailDrawer
      v-model:show="detailDrawerShow"
      :model="activeModel"
    />
  </div>
</template>
