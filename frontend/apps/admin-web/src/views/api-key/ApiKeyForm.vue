<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  useForm,
  FormInput,
  FormInputNumber,
  FormSelect,
  FormDatePicker,
  FormCustom,
  useDialog,
  useDialogClose,
  message,
} from '@repo/hooks'
import { NIcon, type SelectOption } from 'naive-ui'
import { ChevronDown } from '@vicons/ionicons5'
import ApiKeyPlaintextDialog from './ApiKeyPlaintextDialog.vue'
import {
  useStore,
  isoToLocalInput,
  hasAdvancedLimits,
  type ApiKeyFormValues,
  type ApiKeyData,
} from './useStore'

const props = defineProps<{
  /** 编辑回填；新增时为 undefined */
  initial?: ApiKeyData
  /** 租户下拉选项（controller 拉取后传入） */
  tenants: SelectOption[]
  /** 已发布模型别名候选（来自 listPublishes） */
  modelAliases: string[]
}>()

const close = useDialogClose()
const { create, update } = useStore()
const isEdit = computed(() => !!props.initial?.id)

/** 非负数字校验（空串放行=不限/不改） */
const decimalRule = {
  validator: (_rule: unknown, value: string) => !value || /^\d+(\.\d+)?$/.test(value),
  message: '请输入非负数字',
  trigger: 'blur' as const,
}

/** 禁用今天之前的日期（到期时间不能设为过去；今天可选，编辑回填的旧值不受影响） */
const disablePastDate = (ts: number) => ts < new Date().setHours(0, 0, 0, 0)

/** 名称过长时截断（创建成功弹窗标题用；名称最长 128 字符会撑爆标题，保留前 16 字符） */
function truncateName(name: string, max = 16): string {
  return name.length > max ? `${name.slice(0, max)}...` : name
}

/** 已发布模型别名 → n-select tag 选项 */
const modelOptions = computed(() =>
  props.modelAliases.map((m) => ({ label: m, value: m })),
)

/** 高级限流字段（RPM/TPM/配额/预算）默认收起缩短表单；编辑回填有任一值时默认展开，避免已设值不可见 */
const showAdvanced = ref(hasAdvancedLimits(props.initial))

/** 表单底部「展开更多/收起」入口（点击切换高级字段显隐）；热区仅图标+文字，不占整行 */
const renderAdvancedToggle = () =>
  h('div', { class: 'my-[.6rem]' }, [
    h(
      'span',
      {
        class:
          'inline-flex cursor-pointer select-none items-center gap-[.4rem] text-base text-secondary transition-colors hover:text-primary',
        onClick: () => {
          showAdvanced.value = !showAdvanced.value
        },
      },
      [
        // 收起时箭头朝右(点击展开),展开后朝下(点击收起)
        h(
          NIcon,
          {
            class: showAdvanced.value
              ? 'transition-transform'
              : 'transition-transform -rotate-90',
          },
          { default: () => h(ChevronDown) },
        ),
        showAdvanced.value ? '收起' : '展开更多',
      ],
    ),
  ])

const { LmForm, submit } = useForm<ApiKeyFormValues>({
  data: () => ({
    id: props.initial?.id,
    tenant_id: props.initial?.tenant_id ?? null,
    name: props.initial?.name ?? '',
    allowed_models: props.initial?.allowed_models ?? [],
    rpm_limit: props.initial?.rpm_limit ?? null,
    tpm_limit: props.initial?.tpm_limit ?? null,
    total_token_quota: props.initial?.total_token_quota ?? null,
    budget_limit: props.initial?.budget_limit ?? '',
    // UTC ISO → 本地时间字符串回填；提交时 store 层转回 UTC
    expires_at: isoToLocalInput(props.initial?.expires_at ?? null),
  }),
  options: () => [
    FormInput('Key 名称', 'name', {
      rules: [{ required: true, message: '请输入 Key 名称', trigger: 'blur' }],
      attrs: { placeholder: '如：生产环境 key', maxlength: 128, showCount: true },
    }),
    // FormSelect('所属租户', 'tenant_id', props.tenants, {
    //   attrs: { placeholder: '留空=平台直发（不绑定租户，不受租户封禁约束）', clearable: true },
    // }),
    FormSelect('授权模型', 'allowed_models', modelOptions.value, {
      // rules: [{ required: true, type: 'array', message: '请选择或输入授权模型', trigger: 'change' }],
      attrs: {
        placeholder: '选择或输入模型别名(留空=全部模型)',
        multiple: true,
        filterable: true,
        tag: true,
        maxTagCount: 'responsive',
      },
    }),
    FormDatePicker('过期时间', 'expires_at', {
      attrs: {
        type: 'datetime',
        placeholder: '永不过期',
        clearable: true,
        // 本地时间字符串（T 分隔，new Date 按本地解析）；提交时由 store 层转回 UTC
        valueFormat: "yyyy-MM-dd'T'HH:mm:ss",
        // 禁选已过去的日期
        isDateDisabled: disablePastDate,
        class: 'w-full',
      },
    }),
    // 展开更多/收起入口：始终在表单展开项头部
    FormCustom(renderAdvancedToggle),
    // ---- 高级限额：默认收起，底部「展开更多」展开 ----
    FormInputNumber('RPM 限制', 'rpm_limit', {
      visible: showAdvanced,
      attrs: { placeholder: '不限', min: 1, precision: 0, class: 'w-full' },
    }),
    FormInputNumber('TPM 限制', 'tpm_limit', {
      visible: showAdvanced,
      attrs: { placeholder: '不限', min: 1, precision: 0, class: 'w-full' },
    }),
    FormInputNumber('总 Token 配额', 'total_token_quota', {
      visible: showAdvanced,
      attrs: { placeholder: '不限', min: 1, precision: 0, class: 'w-full' },
    }),
    FormInput('预算上限(元)', 'budget_limit', {
      visible: showAdvanced,
      rules: [decimalRule],
      attrs: { placeholder: '不限', type: 'text' },
    }),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    // 校验失败会 throw，弹窗保留
    await validate()
    if (isEdit.value) {
      const { name } = await update(model)
      message.success(`已更新：${name}`)
      close()
    } else {
      const result = await create(model)
      close()
      useDialog({
        title: `已创建：${truncateName(result.api_key.name)}`,
        component: ApiKeyPlaintextDialog,
        props: { plaintext: result.plaintext },
        area: 51,
        btn: false,
        dialogProps: { maskClosable: false },
      })
    }
  },
})

defineExpose({
  onConfirm: (dialogClose: () => void) => submit().then(dialogClose, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
