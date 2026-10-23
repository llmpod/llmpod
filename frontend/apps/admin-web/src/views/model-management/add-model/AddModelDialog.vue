<script setup lang="ts">
import { useWizard } from './useWizard'
import StepSource from './StepSource.vue'
import StepInfo from './StepInfo.vue'
import type { ModelData } from '../useStore'

/** 创建成功回调(quick-setup 向导复用本弹窗时回写新建模型;模型管理页不传) */
const props = defineProps<{
  onCreated?: (record: ModelData) => void
  /** 内嵌模式(quick-setup 空态直接渲染):隐藏内部步骤条与底部按钮,由外层向导按钮驱动 */
  embedded?: boolean
}>()

/** 添加模型向导:模型源 → 模型信息(含确认信息);流程编排见 useWizard,各步骤表单自治 */
const {
  STEPS,
  current,
  sourceStep,
  infoStep,
  inspecting,
  reinspecting,
  inspected,
  feasibility,
  submitting,
  manifestWarnings,
  uploadState,
  close,
  handleInspect,
  prev,
  submit,
  clearManifestWarnings,
} = useWizard(props.onCreated)

/** 内嵌模式驱动接口:外层向导按钮据此推进内部步骤(模板 ref 经 proxyRefs 解包,current 为 number) */
defineExpose({ current, handleInspect, prev, submit })
</script>

<template>
  <div class="flex flex-col gap-[1rem] pt-[.75rem]">
    <n-steps v-if="!props.embedded" size="small" :current="current" status="process">
      <n-step v-for="step in STEPS" :key="step" :title="step" />
    </n-steps>

    <!-- 两步均常驻挂载(v-show)保留表单状态 -->
    <StepSource
      v-show="current === 1"
      ref="sourceStep"
      :inspected="inspected"
      :upload-state="uploadState"
      :manifest-warnings="manifestWarnings"
      @files-change="clearManifestWarnings"
    />
    <StepInfo v-show="current === 2" ref="infoStep" :feasibility="feasibility" />

    <!-- 自定义底部按钮(向导步骤切换,默认 footer 无法满足);内嵌模式隐藏,由外层向导按钮驱动 -->
    <div
      v-if="!props.embedded"
      class="flex justify-end gap-[1.2rem] pt-[1.6rem] border-t border-line"
    >
      <!-- 分片上传中禁用「取消」,避免关弹窗中断上传后状态悬挂 -->
      <n-button v-if="close" :disabled="uploadState.phase !== 'idle'" @click="close"> 取消 </n-button>
      <n-button v-if="current > 1" @click="prev"> 上一步 </n-button>
      <n-button v-if="current === 1" type="primary" :loading="inspecting" @click="handleInspect">
        {{ uploadState.phase !== 'idle' ? '正在上传中' : '探测模型源' }}
      </n-button>
      <n-button v-else type="primary" :loading="submitting || reinspecting" @click="submit">
        确认提交
      </n-button>
    </div>
  </div>
</template>

<style scoped>
:deep(.n-steps) {
  width: 80%;
  margin: 0 auto;
}

:deep(.n-steps .n-step:last-child) {
  flex: 0;
}
</style>
