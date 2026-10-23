import type { Router } from 'vue-router'
import { useDialog } from '@repo/hooks'
import { QUICK_SETUP_PROMPT_KEY, QUICK_SETUP_SKIP_KEY } from '@/api/quick-setup'
import FirstGuideDialog from './FirstGuideDialog.vue'

/**
 * 首次部署引导弹窗入口。
 * 守卫检测 first_guide 落地导航时写入一次性握手标记(守卫阶段 App 未挂载,直接 useDialog
 * 会触发「DialogHost 未挂载」dev 警告),由 BasicLayout 挂载后调用本函数消费并弹窗介绍
 * 「一键部署」向导;不再强制改道路由,是否进入向导由用户点击决定。
 * 底部按钮由弹窗内容自绘(btn: false,样式见 FirstGuideDialog.vue),「开始引导」跳向导、
 * 「稍后再说」仅关闭;任意关闭路径(onClose)统一写 skip 会话标记,本会话不再弹
 * (向导完成时另写 done,两个标记同为守卫的抑制条件)。
 */
export function maybeShowFirstGuide(router: Router): void {
  if (sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY) === null) return
  sessionStorage.removeItem(QUICK_SETUP_PROMPT_KEY)

  const handle = useDialog({
    // 引导窗标题放大一号(走 #header 插槽,LmDialog 默认标题 1.6rem 偏小)
    title: () => h('span', { class: 'text-[2.4rem] font-semibold' }, '欢迎使用'),
    component: FirstGuideDialog,
    area: 60,
    btn: false,
    dialogProps: { maskClosable: false },
    props: {
      onStart: () => {
        handle.close()
        void router.push({ name: 'quick-setup' })
      },
      onLater: () => handle.close(),
    },
    onClose: () => sessionStorage.setItem(QUICK_SETUP_SKIP_KEY, '1'),
  })
}
