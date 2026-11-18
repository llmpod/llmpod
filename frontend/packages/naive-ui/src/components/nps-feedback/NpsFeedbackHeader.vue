<script setup lang="ts">
defineProps<{
  /** 自定义标题；留空时按品牌名拼默认问法 */
  title?: string
  /** 品牌名，用于默认标题里的高亮片段 */
  brand?: string
}>()
</script>

<template>
  <div class="flex items-center gap-[1.2rem]">
    <!-- 装饰插画：明暗各一张，交给 CSS 背景按模式切换，避免两张图同时下载 -->
    <span class="nps-feedback__illustration" aria-hidden="true" />

    <div class="flex min-w-0 flex-col gap-[.6rem] ml-[1.6rem]">
      <!-- 与 LmDialog 默认标题同名，保持外层样式与既有断言一致 -->
      <div class="lm-dialog__title">
        <template v-if="title">
          {{ title }}
        </template>
        <template v-else>
          把 <span class="text-primary">{{ brand }}</span> 推荐给同事，你会打几分？
        </template>
      </div>
      <p class="text-sm leading-[1.5] text-secondary">
        你的评分会直接进入每周的产品复盘，我们十分重视您的反馈。
      </p>
    </div>
  </div>
</template>

<style scoped lang="scss">
// 插画尺寸按浅色素材（288×240）定档，保证明暗两套布局完全一致；
// 深色素材是 288×159 的横向构图，用 contain 居中留白，不会被裁切
.nps-feedback__illustration {
  display: block;
  flex-shrink: 0;
  width: 11.2rem;
  aspect-ratio: 6 / 5;
  background-image: url('../../assets/nps-feedback/illustration-light.webp');
  background-position: center;
  background-repeat: no-repeat;
  background-size: contain;
}

html.dark .nps-feedback__illustration {
  background-image: url('../../assets/nps-feedback/illustration-dark.webp');
}
</style>
