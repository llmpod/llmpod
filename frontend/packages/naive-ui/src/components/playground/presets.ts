/** 预设模版:点击后写入 system prompt。 */
export interface PlaygroundPreset {
  label: string
  prompt: string
}

export const PLAYGROUND_PRESETS: PlaygroundPreset[] = [
  {
    label: '通用对话',
    prompt: '你是一个乐于助人的 AI 助手,回答简洁、准确、友好。',
  },
  {
    label: '代码',
    prompt:
      '你是一名资深程序员,擅长编写和讲解代码。回答时优先给出可运行的代码示例,并附上简要说明。',
  },
  {
    label: '翻译',
    prompt:
      '你是一名专业翻译,请在中文与英文之间互译:输入中文则翻译成英文,输入英文则翻译成中文,只输出译文,保持原文语气与格式。',
  },
  {
    label: '摘要',
    prompt: '你擅长内容摘要,请提炼用户输入的核心要点,用简洁的分点列表输出。',
  },
  {
    label: '创意',
    prompt: '你是一名创意写作助手,思维活跃,擅长头脑风暴、文案创作与故事构思,回答富有想象力。',
  },
]
