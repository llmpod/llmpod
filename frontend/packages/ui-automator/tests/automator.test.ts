// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest'

import {
  InputSimulator,
  UIAutomator,
  findElement,
  generateMousePath,
  isVisible,
  queryElementByString,
  resolveThemeColor,
  sleep,
  waitForCondition,
  waitForElement,
} from '../src'

describe('bezier: generateMousePath', () => {
  it('路径应包含起点与终点，且终点精确相等', () => {
    const from = { x: 0, y: 0 }
    const to = { x: 300, y: 200 }
    const path = generateMousePath(from, to)

    expect(path.length).toBeGreaterThanOrEqual(21)
    expect(path[0]).toEqual(from)
    expect(path[path.length - 1]).toEqual(to)
  })

  it('距离极短时直接返回两点', () => {
    const from = { x: 10, y: 10 }
    const to = { x: 10.5, y: 10.5 }
    expect(generateMousePath(from, to)).toEqual([from, to])
  })
})

describe('dom: 元素查找', () => {
  it('支持 CSS 选择器', () => {
    document.body.innerHTML = '<button id="btn">ok</button>'
    expect(findElement('#btn').textContent).toBe('ok')
  })

  it('支持 XPath 选择器', () => {
    document.body.innerHTML = '<div><span class="target">x</span></div>'
    const el = queryElementByString('//span[@class="target"]')
    expect(el?.textContent).toBe('x')
  })

  it('找不到元素时 findElement 抛错', () => {
    document.body.innerHTML = ''
    expect(() => findElement('#not-exist')).toThrow('找不到元素')
  })

  it('isVisible 对 display:none 返回 false', () => {
    document.body.innerHTML = '<div id="box" style="display:none"></div>'
    expect(isVisible(findElement('#box'))).toBe(false)
  })
})

describe('delay: 等待工具', () => {
  it('sleep 至少等待指定时长', async () => {
    const start = Date.now()
    await sleep(30)
    expect(Date.now() - start).toBeGreaterThanOrEqual(25)
  })

  it('waitForCondition 条件成立时 resolve', async () => {
    let ready = false
    setTimeout(() => { ready = true }, 20)
    await expect(waitForCondition(() => ready, { timeout: 1000, interval: 10 })).resolves.toBeUndefined()
  })

  it('waitForCondition 超时 reject', async () => {
    await expect(waitForCondition(() => false, { timeout: 50, interval: 10 })).rejects.toThrow('超时')
  })

  it('waitForElement 在元素出现后 resolve', async () => {
    document.body.innerHTML = ''
    setTimeout(() => {
      const el = document.createElement('div')
      el.id = 'late'
      // jsdom 默认 rect 全为 0，需模拟尺寸才算“可见”
      el.getBoundingClientRect = () => ({ width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
      document.body.appendChild(el)
    }, 20)

    const el = await waitForElement('#late', { timeout: 1000 })
    expect(el?.id).toBe('late')
  })

  it('waitForElement visible=false 时元素不存在即 resolve null', async () => {
    document.body.innerHTML = ''
    await expect(waitForElement('#ghost', { visible: false })).resolves.toBeNull()
  })
})

describe('InputSimulator', () => {
  it('逐字符输入并触发 input/change 事件', async () => {
    document.body.innerHTML = '<input id="name" />'
    const el = findElement('#name') as HTMLInputElement
    const onInput = vi.fn()
    const onChange = vi.fn()
    el.addEventListener('input', onInput)
    el.addEventListener('change', onChange)

    const keyboard = new InputSimulator({ speed: 0 })
    await keyboard.type('#name', 'abc')

    expect(el.value).toBe('abc')
    expect(onInput).toHaveBeenCalledTimes(3)
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('clear 选项先清空再输入', async () => {
    document.body.innerHTML = '<input id="name" value="old" />'
    const el = findElement('#name') as HTMLInputElement

    const keyboard = new InputSimulator({ speed: 0 })
    await keyboard.type('#name', 'x', { clear: true })

    expect(el.value).toBe('x')
  })

  it('非输入元素抛错', async () => {
    document.body.innerHTML = '<div id="box"></div>'
    const keyboard = new InputSimulator({ speed: 0 })
    await expect(keyboard.type('#box', 'x')).rejects.toThrow('不是可输入元素')
  })
})

describe('resolveThemeColor 主题色解析', () => {
  it('优先读取 --c-primary 完整色值', () => {
    document.documentElement.style.cssText = '--c-primary: #ff0000; --n-primary: 1,2,3;'
    expect(resolveThemeColor('auto')).toBe('#ff0000')
  })

  it('--c-primary 缺失时将 --n-primary RGB 串转为 rgb()', () => {
    document.documentElement.style.cssText = '--n-primary: 24,160,88;'
    expect(resolveThemeColor('auto')).toBe('rgb(24,160,88)')
  })

  it('均无变量时回退默认主色；显式传色则原样返回', () => {
    document.documentElement.style.cssText = ''
    expect(resolveThemeColor('auto')).toBe('#18a058')
    expect(resolveThemeColor('#123456')).toBe('#123456')
  })
})

describe('UIAutomator 引擎', () => {
  it('click 动作触发元素 click 事件并发出 complete', async () => {
    document.body.innerHTML = '<button id="btn">ok</button>'
    const onClick = vi.fn()
    findElement('#btn').addEventListener('click', onClick)

    const bot = new UIAutomator({ humanize: false, maxRetries: 0, cursor: { enabled: false } })
    const onComplete = vi.fn()
    bot.on('complete', onComplete)

    await bot.click('#btn').run()

    expect(onClick).toHaveBeenCalledTimes(1)
    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(bot.getState()).toBe('idle')
  })

  it('元素不存在且无 onError 时抛出错误', async () => {
    document.body.innerHTML = ''
    const bot = new UIAutomator({ humanize: false, maxRetries: 0, retryInterval: 1, cursor: { enabled: false } })
    await expect(bot.click('#missing').run()).rejects.toThrow('找不到元素')
  })

  it('配置 onError 时错误进入回调而不抛出', async () => {
    document.body.innerHTML = ''
    const onError = vi.fn()
    const bot = new UIAutomator({ humanize: false, maxRetries: 0, retryInterval: 1, cursor: { enabled: false }, onError })
    await bot.click('#missing').run()
    expect(onError).toHaveBeenCalledTimes(1)
  })
})
