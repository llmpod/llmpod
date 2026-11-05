<script setup lang="ts">
import { computed, ref } from 'vue'
import { NInput, NRate } from 'naive-ui'

/** 低分档追问：问具体原因 */
const ASK_LOW = '具体是哪里让你不太满意？'
/** 高分档追问：问改进建议 */
const ASK_HIGH = '还有哪一点可以做得更好？'
/** 星级语义标签；下标 0 不使用 */
const SCORE_LABELS = ['', '很不满意', '不太满意', '一般', '比较满意', '非常满意']

const props = defineProps<{
  /**
   * 提交回调：上报等副作用由调用方负责。
   * 抛错时弹窗保持打开，并在下方显示错误提示。
   */
  onSubmit: (score: number, comment: string) => void | Promise<void>
}>()

const score = ref(0)
const comment = ref('')
const submitting = ref(false)
const errorText = ref('')

/** 1–3 星问原因，4–5 星问建议；未打分时用中性提问，不用低分措辞 */
const askText = computed(() => (score.value > 0 && score.value <= 3 ? ASK_LOW : ASK_HIGH))
const scoreLabel = computed(() => (score.value ? SCORE_LABELS[score.value] : '点星星打分'))

/**
 * 由 useDialog 的「提交」按钮触发，close 由 useDialog 注入。
 * 先校验、再提交、成功才关；失败保留弹窗并提示。
 */
async function submit(close: () => void): Promise<void> {
  errorText.value = ''
  if (!score.value) {
    errorText.value = '请先选择一个分数'
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    await props.onSubmit(score.value, comment.value.trim())
    close()
  } catch {
    errorText.value = '提交失败，请稍后再试'
  } finally {
    submitting.value = false
  }
}

defineExpose({ onConfirm: submit })

/** 打分后清掉「请先选择一个分数」 */
function handleScoreChange(): void {
  if (errorText.value) errorText.value = ''
}
</script>

<template>
  <!-- 间距一律用父级 flex + gap：同元素混用 m-0 与 mt-* 会因 CSS 生成顺序互相覆盖 -->
  <div class="flex flex-col gap-[1.6rem]">
    <p class="text-base leading-[1.6] text-default">
      你的评分会直接进入每周的产品复盘，我们按分数逐条看原因。
    </p>

    <div class="flex items-center gap-[1rem]">
      <n-rate
        v-model:value="score"
        size="large"
        :count="5"
        @update:value="handleScoreChange"
      />
      <span class="text-base text-tertiary">{{ scoreLabel }}</span>
    </div>

    <div class="flex flex-col gap-[1rem]">
      <p class="text-base leading-[1.6] text-default">
        {{ askText }}
      </p>

      <n-input
        v-model:value="comment"
        type="textarea"
        :autosize="{ minRows: 3, maxRows: 5 }"
        :maxlength="500"
        show-count
        placeholder="选填，一句话也行"
      />

      <p
        v-if="errorText"
        class="text-base text-error-text"
      >
        {{ errorText }}
      </p>
    </div>
  </div>
</template>
