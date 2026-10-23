<script setup lang="ts">
import { useTheme } from '@repo/hooks'
import {
  CloudUploadOutline,
  CubeOutline,
  RocketOutline,
  ServerOutline,
  TimeOutline,
} from '@vicons/ionicons5'

/** 弹窗动作:开始引导进入向导 / 稍后再说关闭(底部按钮自绘,见 firstGuide.ts 的 btn: false) */
const emit = defineEmits<{ start: []; later: [] }>()

/** 暗色下 --c-primary 仍为深色品牌值(不随明暗变亮),ghost 文字对比度不足,退回默认按钮 */
const { darkMode } = useTheme()

/** 向导四步介绍(与 quick-setup 页 n-steps 标题一致) */
const steps = [
  { icon: ServerOutline, title: '添加节点', desc: '注册 GPU/物理节点,构建算力底座' },
  { icon: CloudUploadOutline, title: '上传模型', desc: '上传模型文件,或从模型市场选择' },
  { icon: CubeOutline, title: '一键部署', desc: '自动编排部署,无需手动配置' },
  { icon: RocketOutline, title: '完成发布', desc: '服务运行后即可对外提供' },
]
</script>

<template>
  <div class="flex flex-col gap-[2.4rem]">
    <p class="m-0 text-small text-secondary leading-[1.7]">
      环境尚未完成初始化,跟随向导在几分钟内完成节点接入、模型上传与服务发布。
    </p>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-[1.2rem]">
      <div
        v-for="step in steps"
        :key="step.title"
        class="flex flex-col gap-[1rem] p-[2rem] rounded-[1rem] border border-line"
      >
        <span
          class="w-[4.8rem] h-[4.8rem] rounded-full bg-primary-soft text-primary flex items-center justify-center"
        >
          <n-icon :component="step.icon" :size="24" />
        </span>
        <span class="text-large font-medium text-strong">{{ step.title }}</span>
        <span class="text-small text-secondary leading-[1.6]">{{ step.desc }}</span>
      </div>
    </div>

    <div class="flex items-center gap-[.6rem] text-small text-tertiary">
      <n-icon class="shrink-0" :component="TimeOutline" :size="18" />
      <span>全程约 5 分钟;也可稍后在「部署管理」页重新进入向导。</span>
    </div>

    <div class="flex justify-end gap-[1.2rem]">
      <!-- 次要按钮:浅色走 ghost 主色描边(贴合设计稿);暗色退回默认按钮,保证文字对比度 -->
      <n-button
        :type="darkMode ? undefined : 'primary'"
        :ghost="!darkMode"
        @click="emit('later')"
      >
        稍后再说
      </n-button>
      <n-button type="primary" @click="emit('start')">开始引导</n-button>
    </div>
  </div>
</template>
