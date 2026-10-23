import { describe, expect, it } from 'vitest'

import {
  engineBrandIcon,
  gpuBrandIcon,
  modelBrandIcon,
} from '@/views/node-pool/topology/components/brandIcons'

/** 取组件名用于断言命中了哪个品牌 */
const nameOf = (component: unknown) => (component as { name?: string } | null)?.name

describe('brandIcons 拓扑节点品牌图标匹配', () => {
  it('推理引擎:vLLM/SGLang/llama.cpp/TGI/Triton 按名称或 key 命中', () => {
    expect(nameOf(engineBrandIcon('vLLM'))).toBe('BrandVllm')
    expect(nameOf(engineBrandIcon('vllm'))).toBe('BrandVllm')
    // display name 未带品牌时由 key 兜底命中
    expect(nameOf(engineBrandIcon('My Serving', 'v-llm'))).toBe('BrandVllm')
    expect(nameOf(engineBrandIcon('SGLang'))).toBe('BrandSglang')

    expect(nameOf(engineBrandIcon('llama.cpp'))).toBe('BrandLlamaCpp')
    expect(nameOf(engineBrandIcon('llama-server'))).toBe('BrandLlamaCpp')
    expect(nameOf(engineBrandIcon('Llama Cpp'))).toBe('BrandLlamaCpp')

    // TGI 为 Hugging Face 产品、Triton 为 NVIDIA 产品,复用对应品牌图标
    expect(nameOf(engineBrandIcon('Text Generation Inference'))).toBe('BrandHuggingFace')
    expect(nameOf(engineBrandIcon('tgi'))).toBe('BrandHuggingFace')
    expect(nameOf(engineBrandIcon('Triton Inference Server'))).toBe('BrandNvidia')
  })

  it('推理引擎:未知引擎与空值返回 null(调用方回退 ionicons)', () => {
    expect(engineBrandIcon('unknown-engine')).toBeNull()
    expect(engineBrandIcon('')).toBeNull()
    expect(engineBrandIcon(null, undefined)).toBeNull()
    // Llama 模型名不应误命中 llama.cpp 引擎规则
    expect(engineBrandIcon('llama-3-8b-instruct')).toBeNull()
  })

  it('模型家族:Qwen/DeepSeek/Gemma/KIMI/MiniMax/GLM 按模型名命中', () => {
    expect(nameOf(modelBrandIcon('Qwen2.5-72B-Instruct'))).toBe('BrandQwen')
    expect(nameOf(modelBrandIcon('通义千问'))).toBe('BrandQwen')
    expect(nameOf(modelBrandIcon('DeepSeek-V3'))).toBe('BrandDeepSeek')
    expect(nameOf(modelBrandIcon('gemma-2-27b-it'))).toBe('BrandGemma')
    expect(nameOf(modelBrandIcon('Kimi-K2-Instruct'))).toBe('BrandKimi')
    expect(nameOf(modelBrandIcon('moonshot-v1-8k'))).toBe('BrandKimi')
    expect(nameOf(modelBrandIcon('MiniMax-Text-01'))).toBe('BrandMiniMax')
    expect(nameOf(modelBrandIcon('GLM-4-Plus'))).toBe('BrandGlm')
    expect(nameOf(modelBrandIcon('chatglm3-6b'))).toBe('BrandGlm')
    expect(nameOf(modelBrandIcon('智谱清言'))).toBe('BrandGlm')
    // 模型与接口生态
    expect(nameOf(modelBrandIcon('Hugging Face Hub 镜像'))).toBe('BrandHuggingFace')
  })

  it('模型家族:未收录模型与空值返回 null', () => {
    expect(modelBrandIcon('llama-3-8b')).toBeNull()
    expect(modelBrandIcon('bge-m3')).toBeNull()
    expect(modelBrandIcon('')).toBeNull()
  })

  it('GPU:NVIDIA 按品牌词与常见型号命中', () => {
    expect(nameOf(gpuBrandIcon('NVIDIA GeForce RTX 4090'))).toBe('BrandNvidia')
    expect(nameOf(gpuBrandIcon('Tesla V100-SXM2-16GB'))).toBe('BrandNvidia')
    expect(nameOf(gpuBrandIcon('A100 80GB PCIe'))).toBe('BrandNvidia')
    expect(nameOf(gpuBrandIcon('RTX 4090'))).toBe('BrandNvidia')
    expect(nameOf(gpuBrandIcon('H100'))).toBe('BrandNvidia')
    expect(nameOf(gpuBrandIcon('H20'))) .toBe('BrandNvidia')
  })

  it('GPU:华为昇腾按 ascend/atlas/型号别名命中', () => {
    expect(nameOf(gpuBrandIcon('Ascend 910B'))).toBe('BrandAscend')
    expect(nameOf(gpuBrandIcon('华为昇腾 910B3'))).toBe('BrandAscend')
    expect(nameOf(gpuBrandIcon('Atlas 300I Duo'))).toBe('BrandAscend')
  })

  it('GPU:未知型号返回 null;型号词不误伤异厂名称', () => {
    expect(gpuBrandIcon('AMD Instinct MI250X')).toBeNull()
    expect(gpuBrandIcon('')).toBeNull()
    // Ascend 名称不应误命中 NVIDIA 型号词(a10 等)
    expect(nameOf(gpuBrandIcon('Ascend 910B'))).toBe('BrandAscend')
    expect(nameOf(gpuBrandIcon('Atlas 800I A2'))).toBe('BrandAscend')
  })
})
