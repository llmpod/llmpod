<script setup lang="ts">
import { onMounted } from 'vue'
import { useStore, formatDecimal } from './useStore'

const { wallet, walletLoading, fetchWallet } = useStore()

onMounted(() => {
  void fetchWallet()
})

/** 余额卡片定义(金额均为 Decimal 字符串,去尾零展示) */
const cards = computed(() => [
  {
    label: '总余额(元)',
    value: formatDecimal(wallet.value?.balance ?? null),
    hint: '只被充值/调额/真实扣费改动',
  },
  {
    label: '可用余额(元)',
    value: formatDecimal(wallet.value?.available ?? null),
    hint: '= 总余额 - 在途冻结',
  },
])
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <!-- 窄屏单列:金额较长,2 列在手机上会挤压换行 -->
    <div class="grid grid-cols-1 gap-[.75rem] md:grid-cols-2">
      <LmCard
        v-for="card in cards"
        :key="card.label"
      >
        <n-skeleton
          v-if="walletLoading"
          text
          style="display: block; width: 4.8rem; height: 1.9rem; font-size: 12px; margin: 1.2px 0"
        />
        <div
          v-else
          class="text-xs text-secondary"
        >
          {{ card.label }}
        </div>
        <n-skeleton
          v-if="walletLoading"
          text
          style="display: block; width: 6rem; height: 2rem; font-size: 20px; margin-top: 10px"
        />
        <div
          v-else
          class="mt-[.25rem] text-xl font-semibold tabular-nums text-strong"
        >
          {{ card.value }}
        </div>
        <div class="mt-[.5rem] text-xs text-secondary">
          {{ card.hint }}
        </div>
      </LmCard>
    </div>

    <!-- 暂时隐藏:租户端不支持自助充值,恢复时取消注释即可
    <LmCard
      title="充值说明"
      segmented
    >
      <n-text
        depth="2"
        class="text-sm leading-relaxed"
      >
        充值与额度调整由平台运营完成。如需充值或调整额度,请联系平台管理员,调整完成后此页面与流水记录将实时更新。
      </n-text>
    </LmCard>
    -->
  </div>
</template>
