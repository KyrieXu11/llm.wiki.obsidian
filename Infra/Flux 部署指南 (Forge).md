---
tags:
  - infra
  - deployment
  - image-generation
  - flux
  - forge
created: "2026-04-15"
aliases:
  - Flux Deployment
  - Flux on Forge
---

# Flux 部署指南 (Forge)

## 概述

Flux 是 Black Forest Labs（Stable Diffusion 原班人马）推出的图像生成模型，基于 DiT（Diffusion Transformer）架构，prompt following 能力显著强于 SDXL。Forge（stable-diffusion-webui-forge）已原生支持 Flux，可直接复用现有环境。

## 模型版本选择

| 版本 | 许可 | 参数量 | 权重大小 | 说明 |
|------|------|--------|---------|------|
| `FLUX.1-schnell` | Apache 2.0 | 12B | ~23GB (fp16) | 4 步出图，速度优先 |
| `FLUX.1-dev` | 非商用研究 | 12B | ~23GB (fp16) | 质量最好的开放版本 |
| `FLUX.1-pro` | API only | — | — | 仅通过 BFL API 调用 |

> H20（96GB HBM3）显存充裕，直接用 fp16 全量权重即可，无需量化。

## 下载模型

### Flux 主模型

```bash
# FLUX.1-dev（推荐）
hf download black-forest-labs/FLUX.1-dev \
  --local-dir /path/to/forge/models/Stable-diffusion/flux1-dev

# FLUX.1-schnell（快速迭代调 prompt 时用）
hf download black-forest-labs/FLUX.1-schnell \
  --local-dir /path/to/forge/models/Stable-diffusion/flux1-schnell
```

> 注意：`FLUX.1-dev` 需要先在 Hugging Face 上同意 license 并登录：`hf login`

### Text Encoder（T5-XXL）

Flux 使用 CLIP-L + T5-XXL 双 text encoder，Forge 需要 T5 权重：

```bash
hf download comfyanonymous/flux_text_encoders \
  --local-dir /path/to/forge/models/text_encoder/
```

文件列表：
- `t5xxl_fp16.safetensors`（~9.5GB）— fp16 全量
- `clip_l.safetensors`（~234MB）

### VAE

Flux 使用专用 VAE，Forge 通常会自动加载。如果需要手动指定：

```bash
hf download black-forest-labs/FLUX.1-dev flux1-dev.safetensors ae.safetensors \
  --local-dir /path/to/forge/models/VAE/
```

## Forge 配置

### WebUI 参数

在 Forge WebUI 中：

1. **Checkpoint** 下拉选择 flux1-dev（或 schnell）
2. Forge 自动识别 Flux 架构，切换采样流程

### 推荐生成参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| Steps | 20-28 | schnell 可低至 4 步 |
| CFG Scale | 1.0 | Flux 使用 guidance distillation，不依赖高 CFG |
| Sampler | Euler | Flux 原生采样器 |
| 分辨率 | 1024x1024 | 支持非正方形，如 768x1344 |
| Negative Prompt | **留空** | Flux 架构不支持传统 negative prompt |

### API 调用

Forge 的 `/sdapi/v1/txt2img` 端点在 Flux 下仍可用：

```json
{
  "prompt": "pixel art game sprite, centered single clownfish, side view, clean silhouette, limited 16 color palette, crisp pixel edges, no anti-aliasing",
  "width": 1024,
  "height": 1024,
  "steps": 24,
  "cfg_scale": 1.0,
  "sampler_name": "Euler",
  "seed": -1
}
```

与 SDXL 的关键差异：
- 去掉 `negative_prompt`
- 去掉 `lora_tag`（SDXL LoRA 不兼容）
- `cfg_scale` 设为 `1.0`
- `sampler_name` 改为 `Euler`

可通过 `override_settings` 在 API 中指定模型，无需在 WebUI 手动切换：

```json
{
  "override_settings": {
    "sd_model_checkpoint": "flux1-dev"
  }
}
```

## 架构差异：Flux vs SDXL

| 特性 | SDXL | Flux |
|------|------|------|
| 骨干网络 | UNet | DiT (Diffusion Transformer) |
| Text Encoder | CLIP-L + CLIP-G | CLIP-L + T5-XXL |
| Negative Prompt | 支持 | 不支持 |
| CFG Scale | 5-8 | 1.0（guidance distillation） |
| LoRA 兼容 | SDXL LoRA | Flux 专用 LoRA |
| Prompt Following | 中等 | 强（受益于 T5-XXL） |

**重要：SDXL LoRA 与 Flux 不兼容。** 例如 `nerijs/pixel-art-xl` 是 SDXL LoRA，无法在 Flux 上使用。需要寻找 Flux 专用的 LoRA，或依赖 Flux 更强的 prompt following 能力通过纯文本描述达成风格。

## Pixel Art 风格迁移方案

从 SDXL + pixel-art-xl 迁移到 Flux 时，有三种路线：

### 方案 A：纯 Prompt 驱动（推荐先试）

Flux 的 T5-XXL encoder 对风格描述的理解远强于 SDXL，很多时候不需要 LoRA：

```
pixel art game sprite, 16-bit retro style, limited 12-16 color palette,
crisp pixel edges, no anti-aliasing, centered single [species],
side view, clean silhouette, solid color background
```

### 方案 B：Flux 专用 Pixel Art LoRA

社区已有 Flux pixel art LoRA，可在 Civitai 搜索 "pixel art flux" 筛选。下载后放到 `models/Lora/`，在 prompt 中用 `<lora:name:weight>` 触发。

### 方案 C：混合流程

- 用 Flux 生成高质量构图和色彩方案
- 用 SDXL + pixel-art-xl 做最终像素化渲染
- 或者 Flux 出图后用后处理（最近邻缩放 + 调色板量化）实现像素风格

## 启动与验证

```bash
# 1. 启动 Forge（确保 GPU 可见）
cd /path/to/forge
python launch.py --listen --port 7860

# 2. 在 WebUI 中选择 flux1-dev checkpoint

# 3. 测试生成
curl -s http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "pixel art, a cute clownfish, side view, clean outline, limited palette",
    "width": 1024,
    "height": 1024,
    "steps": 24,
    "cfg_scale": 1.0,
    "sampler_name": "Euler"
  }' | python3 -c "
import sys, json, base64
data = json.load(sys.stdin)
img = base64.b64decode(data['images'][0])
with open('test_flux.png', 'wb') as f:
    f.write(img)
print('Saved test_flux.png')
"
```

## 常见问题

### T5 加载 OOM
H20 不会遇到此问题。低显存卡可用 `t5xxl_fp8_e4m3fn.safetensors`（~4.7GB）替代。

### 出图模糊 / 质量差
检查 CFG Scale 是否过高。Flux 在 CFG > 2.0 时画面会过饱和/失真，保持 1.0 即可。

### API 返回空图
确认 Forge 前端已加载 Flux checkpoint。首次切换模型可能需要等待加载完成。

## 参考资料

- [FLUX.1-dev (Hugging Face)](https://huggingface.co/black-forest-labs/FLUX.1-dev)
- [FLUX.1-schnell (Hugging Face)](https://huggingface.co/black-forest-labs/FLUX.1-schnell)
- [Forge GitHub](https://github.com/lllyasviel/stable-diffusion-webui-forge)
- [[FLUX.2 部署指南 (ComfyUI)|FLUX.2 部署指南]] — 下一代模型，推荐用 ComfyUI 部署
- [[Deployment MOC|部署推理]]
- [[Infra MOC|基础设施]]
