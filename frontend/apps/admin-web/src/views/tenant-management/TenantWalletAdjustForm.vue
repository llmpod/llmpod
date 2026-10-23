<script setup lang="ts">
import { useForm, FormInput, FormInputNumber, FormRadioButtonGroup, useDialogClose, message } from '@repo/hooks'
import {
  formatDecimal,
  subtractDecimal,
  useStore,
  type Tenant,
  type WalletAdjustFormValues,
  type WalletAdjustMode,
} from './useStore'

/** 当前行租户;调额成功后经 store.notify 联动列表/详情/统计卡刷新 */
const props = defineProps<{
  tenant: Tenant
}>()

const close = useDialogClose()
const { adjustWallet } = useStore()

const MODE_OPTIONS = [
  { label: '增加', value: 'add' },
  { label: '扣减', value: 'subtract' },
  { label: '覆盖为', value: 'override' },
]

const { LmForm, submit, params } = useForm<WalletAdjustFormValues>({
  data: () => ({
    mode: 'add' as WalletAdjustMode,
    amount: null as number | null,
    reason: '',
  }),
  options: () => [
    FormRadioButtonGroup('调整方式', 'mode', MODE_OPTIONS),
    FormInputNumber('金额(元)', 'amount', {
      rules: [
        { required: true, type: 'number', message: '请输入金额', trigger: ['blur', 'change'] },
        {
          // 增加扣减必须填变动额(空值交给 required,覆盖为允许 0);显式返回注解避免与 params 的循环推断
          validator: (_rule: unknown, value: number | null): boolean =>
            value === null || params().mode === 'override' || value > 0,
          message: '增加/扣减时金额需大于 0',
          trigger: ['blur', 'change'],
        },
      ],
      attrs: { precision: 2, placeholder: '增加/扣减填变动额,覆盖填目标余额',class:'w-[22rem]' },
    }),
    FormInput('调额原因', 'reason', {
      attrs: { placeholder: '可选,写入流水记录' },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    const wallet = await adjustWallet(props.tenant.id, {
      amount: model.amount as number,
      mode: model.mode,
      reason: model.reason || null,
    })
    // Decimal 字符串十进制减法,禁浮点(浮点会得 424.09999999999997 一类脏尾)
    const available = subtractDecimal(wallet.balance, wallet.frozen_balance)
    message.success(`已调整:${props.tenant.name} 可用余额 ¥${formatDecimal(available)}`)
    close()
  },
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <div class="mb-[.75rem] text-small text-secondary">
    当前余额:总 {{ formatDecimal(tenant.balance) }} 元 · 冻结
    {{ formatDecimal(tenant.frozen_balance) }} 元 · 可用 {{ formatDecimal(tenant.available) }} 元
  </div>
  <LmForm />
</template>
