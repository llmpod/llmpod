<script setup lang="ts">
const okIcon = `${import.meta.env.BASE_URL}icons/cost/ok.svg`

/** 套餐(静态) */
interface Plan {
  name: string
  price: string
  tokens: string
  features: string[]
  recommended?: boolean
  contactSales?: boolean
}
const plans: Plan[] = [
  {
    name: '基础包',
    price: '¥99',
    tokens: '1000万 Token',
    features: ['Token', '基础模型', '部署2个'],
  },
  {
    name: '专业包',
    price: '¥299',
    tokens: '5000万 Token',
    features: ['Token', '优先队列', '高级模型', '部署10个'],
    recommended: true,
  },
  {
    name: '企业包',
    price: '¥999',
    tokens: '3亿 Token',
    features: ['全部模型', '独享节点', '专属客服', 'SLA 99.99%'],
    contactSales: true,
  },
]

/** 卡片内容:flex 列布局,按钮 mt-auto 底部对齐 */
const planContentStyle: Record<string, string> = {
  padding: '24px',
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
}
</script>

<template>
  <!-- 窄屏单列:套餐卡内容多(价格/特性列表/按钮),3 列在手机上严重挤压 -->
  <div class="grid grid-cols-1 gap-[1.2rem] md:grid-cols-3">
    <LmCard
      v-for="plan in plans"
      :key="plan.name"
      bordered
      class="plan-card relative"
      :class="{ 'plan-card--recommended': plan.recommended }"
      :content-style="planContentStyle"
    >
      <!-- 推荐角标:-2px 外移压住推荐卡的 2px 描边 -->
      <div
        v-if="plan.recommended"
        class="absolute -right-[2px] -top-[2px] rounded-bl-[8px] rounded-tr-[8px] bg-primary px-[1.2rem] py-[0.5rem] text-xs leading-[1.2] text-inverse"
      >
        推荐
      </div>

      <div class="text-sm font-semibold text-strong">
        {{ plan.name }}
      </div>
      <div class="mt-[0.8rem] text-2xl font-semibold leading-none tabular-nums text-strong">
        {{ plan.price }}
      </div>
      <div class="mt-[0.8rem] text-xs text-secondary">
        {{ plan.tokens }}
      </div>

      <n-divider class="!mb-[2rem] !mt-[2rem]" />

      <div class="flex flex-col gap-[1.2rem]">
        <div
          v-for="feature in plan.features"
          :key="feature"
          class="flex items-center gap-[0.8rem] text-sm text-default"
        >
          <img :src="okIcon" alt="" class="h-[1.4rem] w-[1.4rem] shrink-0" />
          <span>{{ feature }}</span>
        </div>
      </div>

      <div class="mt-auto pt-[2.4rem]">
        <n-button
          :type="plan.recommended ? 'primary' : 'default'"
          block
          :class="!plan.recommended && '!border-none !bg-fill-4 !text-secondary'"
        >
          {{ plan.contactSales ? '联系销售' : '购买' }}
        </n-button>
      </div>
    </LmCard>
  </div>
</template>

<style scoped>
/* 推荐套餐:主色 2px 描边。双类名提升优先级,压过 naive-ui 运行时注入的卡片边框样式 */
.plan-card.plan-card--recommended {
  border: 2px solid var(--c-primary);
}
:deep(.n-card-content) {
  padding: 3rem !important;
}
</style>
