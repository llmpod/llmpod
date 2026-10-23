/**
 * 键盘输入模拟器
 * @description 模拟真实用户键盘输入，兼容 Vue/React 双向绑定
 * 完整事件序列：focus → keydown → keypress → input (native setter) → keyup → change
 */

import type { InputSimulatorOptions, InputOptions, Selector } from '../types'
import { sleep, addNoise } from '../utils/delay'
import { findElement, dispatchKeyEvent, dispatchInputEvent, dispatchChangeEvent, focusElement, getNativeInputSetter } from '../utils/dom'

/** 特殊键定义 */
interface SpecialKey {
  key: string
  code: string
  keyCode: number
}

/** 常用特殊键映射 */
const SPECIAL_KEYS: Record<string, SpecialKey> = {
  Backspace: { key: 'Backspace', code: 'Backspace', keyCode: 8 },
  Tab: { key: 'Tab', code: 'Tab', keyCode: 9 },
  Enter: { key: 'Enter', code: 'Enter', keyCode: 13 },
  Escape: { key: 'Escape', code: 'Escape', keyCode: 27 },
  Delete: { key: 'Delete', code: 'Delete', keyCode: 46 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
  Home: { key: 'Home', code: 'Home', keyCode: 36 },
  End: { key: 'End', code: 'End', keyCode: 35 }
}

/** 输入序列 token:字面字符,或 SPECIAL_KEYS 中的特殊键名 */
interface TypeToken {
  char: string
  /** true 时 char 为特殊键名(按下按键),false 为字面字符(打字) */
  special: boolean
}

/**
 * 文本转输入 token 序列:{KeyName} 命中特殊键表则作为按键指令,{{ 为字面 {;
 * 未命中/未闭合的花括号按字面字符处理,纯文本调用行为不变
 */
function tokenizeTypeText(text: string): TypeToken[] {
  const tokens: TypeToken[] = []
  let i = 0
  while (i < text.length) {
    if (text[i] === '{') {
      if (text.startsWith('{{', i)) {
        tokens.push({ char: '{', special: false })
        i += 2
        continue
      }
      const close = text.indexOf('}', i + 1)
      const name = close > i ? text.slice(i + 1, close) : ''
      if (name in SPECIAL_KEYS) {
        tokens.push({ char: name, special: true })
        i = close + 1
        continue
      }
    }
    tokens.push({ char: text[i], special: false })
    i += 1
  }
  return tokens
}

/**
 * 键盘输入模拟器
 * 支持逐字符打字、直接设置值、清空、粘贴等操作
 * 特别兼容 Vue v-model (通过原生 setter + input 事件) 和 React onChange
 */
export class InputSimulator {
  private defaultSpeed: number
  private debug: boolean

  constructor(options: InputSimulatorOptions = {}) {
    this.defaultSpeed = options.speed ?? 80
    this.debug = options.debug ?? false
  }

  /** 打印调试日志 */
  private log(...args: unknown[]): void {
    if (this.debug) {
      console.log('[InputSimulator]', ...args)
    }
  }

  /**
   * 获取输入元素（支持 input 和 textarea）
   */
  private getInputElement(selector: Selector): HTMLInputElement | HTMLTextAreaElement {
    const el = findElement(selector)
    if (!(el instanceof HTMLInputElement) && !(el instanceof HTMLTextAreaElement)) {
      throw new Error(`UIAutomator: 元素 "${String(selector)}" 不是可输入元素`)
    }
    return el
  }

  /**
   * 通过原生 setter 设置输入框的值
   * 兼容 Vue/React 双向绑定：普通赋值 el.value = x 不会触发框架的状态更新
   * 必须通过 HTMLInputElement.prototype 的原生 setter 设置，再手动触发 input 事件
   */
  private setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
    // 尝试使用原生 setter（Vue/React 兼容）
    const nativeSetter = getNativeInputSetter(el)
    if (nativeSetter) {
      nativeSetter(value)
    } else {
      // 降级：直接赋值
      el.value = value
    }
  }

  /**
   * 清空输入框内容
   */
  clear(el: HTMLInputElement | HTMLTextAreaElement): void {
    this.setNativeValue(el, '')
    dispatchInputEvent(el)
    this.log(`清空输入框`)
  }

  /**
   * 逐字符打字输入
   * 对每个字符触发完整的键盘事件序列：
   * keydown → keypress → (更新value) → input → keyup
   *
   * 特殊键转义：{KeyName}（如 {Backspace}/{Enter}）命中特殊键表时按下对应键并执行其行为
   * （Backspace 删末位字符），{{ 输出字面 {，其余文本按字面逐字符输入。
   *
   * @param selector 目标输入元素
   * @param text 要输入的文本（可含 {KeyName} 特殊键转义）
   * @param options 输入选项
   */
  async type(selector: Selector, text: string, options: InputOptions = {}): Promise<void> {
    const { speed, clear = false, typeMode = true } = options
    const charSpeed = speed ?? this.defaultSpeed

    this.log(`输入文本 "${text}" 到 ${String(selector)}`)

    const el = this.getInputElement(selector)

    // 聚焦元素
    focusElement(el)
    await sleep(30)

    // 如果需要先清空
    if (clear) {
      this.clear(el)
      await sleep(50)
    }

    if (!typeMode) {
      // 快速模式：直接设置值
      await this.setValue(el, text)
      return
    }

    // 逐字符输入({KeyName} 特殊键转义经 tokenize 展开为独立按键 token)
    let currentValue = el.value

    for (const token of tokenizeTypeText(text)) {
      const isSpecial = token.special
      const keyInfo = isSpecial
        ? SPECIAL_KEYS[token.char]
        : { key: token.char, code: `Key${token.char.toUpperCase()}`, keyCode: token.char.charCodeAt(0) }

      // keydown
      dispatchKeyEvent(el, 'keydown', {
        key: keyInfo.key,
        code: keyInfo.code,
        keyCode: keyInfo.keyCode
      })

      // keypress（只对可打印字符）
      if (!isSpecial) {
        dispatchKeyEvent(el, 'keypress', {
          key: keyInfo.key,
          code: keyInfo.code,
          keyCode: keyInfo.keyCode,
          charCode: token.char.charCodeAt(0)
        })
      }

      // 处理特殊键行为:退格删末位(空串 no-op);其余特殊键不改值
      if (isSpecial && token.char === 'Backspace') {
        currentValue = currentValue.slice(0, -1)
      } else if (!isSpecial) {
        currentValue += token.char
      }

      // 通过原生 setter 更新值并触发 input 事件（Vue/React 双向绑定兼容）
      this.setNativeValue(el, currentValue)
      dispatchInputEvent(el)

      // keyup
      dispatchKeyEvent(el, 'keyup', {
        key: keyInfo.key,
        code: keyInfo.code,
        keyCode: keyInfo.keyCode
      })

      // 每个字符之间的随机延迟（模拟真实打字节奏）
      const delay = addNoise(charSpeed, 0.25)
      await sleep(delay)
    }

    // 最终触发 change 事件
    dispatchChangeEvent(el)

    this.log(`输入完成，最终值: "${el.value}"`)
  }

  /**
   * 直接设置输入框的值（不逐字符，但兼容框架双向绑定）
   * @param selector 目标元素
   * @param value 要设置的值
   */
  async setValue(selector: Selector, value: string): Promise<void> {
    this.log(`直接设置值 "${value}" 到 ${String(selector)}`)

    const el = this.getInputElement(selector)
    focusElement(el)

    // 设置新值
    this.setNativeValue(el, value)
    dispatchInputEvent(el)
    dispatchChangeEvent(el)

    this.log(`设置值完成`)
  }

  /**
   * 模拟粘贴操作
   * 触发 paste 事件并设置 clipboardData（受浏览器安全策略限制）
   * 实际使用 DataTransfer 模拟粘贴内容
   */
  async paste(selector: Selector, text: string): Promise<void> {
    this.log(`粘贴文本 "${text}" 到 ${String(selector)}`)

    const el = this.getInputElement(selector)
    focusElement(el)
    await sleep(30)

    // 构造 ClipboardEvent
    const dt = new DataTransfer()
    dt.setData('text/plain', text)

    const pasteEvent = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: dt
    })

    // 先触发 paste 事件（允许业务代码拦截）
    const cancelled = !el.dispatchEvent(pasteEvent)

    if (!cancelled) {
      // 如果 paste 未被拦截，则直接设置值
      const currentVal = el.value
      // 获取当前光标位置
      const start = el.selectionStart ?? currentVal.length
      const end = el.selectionEnd ?? currentVal.length
      const newValue = currentVal.slice(0, start) + text + currentVal.slice(end)

      this.setNativeValue(el, newValue)
      dispatchInputEvent(el)
      dispatchChangeEvent(el)
    }

    this.log(`粘贴完成`)
  }

  /**
   * 按下指定特殊键
   * @param selector 目标元素
   * @param key 键名（如 'Enter', 'Escape', 'Tab'）
   */
  async pressKey(selector: Selector, key: string): Promise<void> {
    this.log(`按键 ${key} 到 ${String(selector)}`)

    const el = findElement(selector)
    focusElement(el)

    const keyInfo = SPECIAL_KEYS[key] ?? {
      key,
      code: `Key${key.toUpperCase()}`,
      keyCode: key.charCodeAt(0)
    }

    dispatchKeyEvent(el, 'keydown', keyInfo)
    if (key === 'Enter') {
      el.dispatchEvent(new Event('submit', { bubbles: true }))
    }
    await sleep(50)
    dispatchKeyEvent(el, 'keyup', keyInfo)
  }
}
