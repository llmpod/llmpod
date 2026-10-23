<script setup lang="ts">
import BalanceOverview from './BalanceOverview.vue'
// 暂时隐藏:套餐购买/账单管理/发票管理,恢复时取消下方注释即可
// import BillManage from './BillManage.vue'
// import InvoiceManage from './InvoiceManage.vue'
// import PlanPurchase from './PlanPurchase.vue'
import TransactionList from './TransactionList.vue'
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <BalanceOverview />
    <TransactionList />
  </div>
</template>
