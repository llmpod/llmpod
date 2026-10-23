<script setup lang="ts">
import { computed, h } from 'vue'
import { NTag } from 'naive-ui'
import { FormInput, FormSelect, useForm, type UseFormReturn } from '@repo/hooks'
import {
  ENGINE_LABEL,
  MODEL_TYPE_OPTIONS,
  QUANTIZATION_OPTIONS,
  type FeasibilityReport,
} from '../useStore'
import type { InfoFormValues } from './useWizard'

const props = defineProps<{
  /** 可部署性检测报告(引擎部署表格;随识别结果一并更新) */
  feasibility: FeasibilityReport | null
}>()

// 识别结果预填由向导在探测成功后写入 params();format/architecture 不展示不编辑,仅随提交回传;size_gb/is_moe 经名称项 feedback 插槽以 tag 形式只读回显
const form: UseFormReturn<InfoFormValues> = useForm<InfoFormValues>({
  data: () => ({
    name: '',
    model_type: '',
    format: null,
    architecture: '',
    size_gb: null,
    quantization: null,
    is_moe: false,
  }),
  options: () => [
    FormInput('模型名称', 'name', {
      rules: [{ required: true, message: '请输入模型名称', trigger: 'blur' }],
      attrs: { placeholder: '请输入模型显示名' },
      // 探测回显挂名称项底部(feedback 插槽,无校验时为空状态),随表单项缩进,不另起分组
      slots: { feedback: () => renderInspectEcho() },
    }),
    FormSelect('模型类别', 'model_type', MODEL_TYPE_OPTIONS, {
      rules: [{ required: true, message: '请选择模型类别', trigger: 'change' }],
      attrs: { clearable: false },
    }),
    FormSelect('量化方案', 'quantization', QUANTIZATION_OPTIONS, {
      attrs: { placeholder: '未识别' },
    }),
    // FormSwitch('MoE 架构', 'is_moe'),
  ],
  layout: { labelPlacement: 'top' },
})

const { LmForm, validate, params } = form

/**
 * 探测回显:权重大小 + MoE 架构,仅两枚 tag,无文字 label。
 * 权重未识别时只保留架构 tag;两个值均无时返回 null,不渲染空 feedback 容器。
 * 名称必填校验未通过(为空)时同样返回 null,把 feedback 区让给校验错误文案;
 * naive-ui 的 feedback 插槽会整体覆盖 renderExplains,不主动让位则错误信息无法显示。
 * 注:当前 name 仅 required 规则,故以 name 为空近似校验失败;若后续新增格式校验需另行处理。
 */
function renderInspectEcho() {
  const { name, size_gb, is_moe } = params()
  if (!name.trim() || (size_gb == null && !is_moe)) return null
  return h('div', { class: 'inspect-echo' }, [
    size_gb != null &&
      h(
        NTag,
        { size: 'tiny', type: 'info', bordered: false, class: 'mr-[1rem]' },
        { default: () => `${size_gb}GB` },
      ),
    is_moe && h(NTag, { size: 'tiny', type: 'info', bordered: false }, { default: () => 'MoE' }),
  ])
}

/** 引擎部署评估列表 */
const engineItems = computed(() => props.feasibility?.engines ?? [])

// 向导父级据此校验并读取表单值
defineExpose({ validate, params })
</script>

<template>
  <div class="flex flex-col gap-[.75rem]">
    <n-alert v-if="feasibility && !feasibility.deployable" type="warning" title="部署预检未通过">
      {{ feasibility.message || '当前集群资源/引擎条件下该模型不可部署' }}
    </n-alert>
    <LmForm />
    <!-- 引擎部署:表格展示各引擎支持判定 -->
    <div v-if="engineItems.length" class="flex flex-col gap-[1.6rem]">
      <n-text class="info-group-title"> 引擎部署 </n-text>
      <n-table class="text-base" size="small" :bordered="true" :single-line="false">
        <thead>
          <tr>
            <th class="engine-col-name">引擎</th>
            <th class="engine-col-status">状态</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in engineItems" :key="item.engine">
            <td>{{ ENGINE_LABEL[item.engine] ?? item.engine }}</td>
            <td>
              <n-tag
                :type="
                  item.supported === true
                    ? 'success'
                    : item.supported === false
                      ? 'error'
                      : 'default'
                "
                size="small"
                :bordered="false"
              >
                {{
                  item.supported === true
                    ? '可部署'
                    : item.supported === false
                      ? '不可部署'
                      : '无法判定'
                }}
              </n-tag>
            </td>
            <td>
              <n-text class="engine-reasons">
                {{ item.reasons?.length ? item.reasons.join(';') : '—' }}
              </n-text>
            </td>
          </tr>
        </tbody>
      </n-table>
    </div>
  </div>
</template>

<style scoped>
/* 分组标题字重;引擎/状态列定宽,原因长文本允许换行 */
.info-group-title {
  @apply text-strong text-large;
  font-weight: 500;
}

/* 探测回显:两枚 tiny tag 并列,随表单项缩进,视觉层级低于表单控件 */
.inspect-echo {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  padding-top: 0.4rem;
}

.engine-col-name {
  width: 7rem;
}

.engine-col-status {
  width: 6rem;
}

.engine-reasons {
  line-height: 1.5;
  word-break: break-all;
}
</style>
