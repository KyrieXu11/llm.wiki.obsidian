---
tags:
  - infra
  - deployment
  - image-generation
  - flux
  - comfyui
created: "2026-04-15"
aliases:
  - FLUX.2 Deployment
  - FLUX.2 on ComfyUI
  - Flux2 ComfyUI
---

# FLUX.2 部署指南 (ComfyUI)

## 概述

FLUX.2 是 Black Forest Labs 于 2026 年发布的第二代图像生成模型，相比 [[Flux 部署指南 (Forge)|FLUX.1]] 有架构级别的变化：

- 骨干从 12B 升级到 **32B** 参数（DiT 架构重新设计）
- Text Encoder 从 CLIP-L + T5-XXL 换成 **Mistral Small 3.1**（24B 多模态 VLM）
- 全新 VAE（`AutoencoderKLFlux2`）
- 文字渲染、prompt adherence、材质光照理解显著提升
- 支持多参考图输入（最多 10 张）

**Forge 对 FLUX.2-dev 支持不完善**，推荐使用 ComfyUI 部署。

## FLUX.2 模型家族

| 版本 | 参数量 | Text Encoder | Steps | VRAM | 许可 | 特点 |
|------|--------|-------------|-------|------|------|------|
| **FLUX.2-dev** | 32B | Mistral Small 3.1 (24B) | 50 (28 可接受) | 80GB+ (bf16) | 非商用 | 最高质量 |
| **FLUX.2-klein-9B** | 9B | Qwen3 8B | 4 | ~29GB | 非商用 | 亚秒出图 |
| **FLUX.2-klein-4B** | 4B | Qwen3 4B | 4 | ~13GB | Apache 2.0 | 消费级显卡可跑 |
| **FLUX.2-small-decoder** | ~28M (仅 VAE) | — | — | — | Apache 2.0 | 解码加速 1.4x |

## 与 FLUX.1 的架构对比

| 特性 | FLUX.1 | FLUX.2 |
|------|--------|--------|
| 参数量 | 12B | 32B |
| Transformer 结构 | 19 double + 38 single stream | 8 double + 48 single stream |
| Text Encoder | CLIP-L + T5-XXL | Mistral Small 3.1 (单编码器) |
| 激活函数 | GELU | SwiGLU |
| VAE | `ae.safetensors` | `AutoencoderKLFlux2`（重新训练） |
| CFG / Guidance | 1.0 | 4.0 |
| LoRA 兼容 | FLUX.1 专用 | FLUX.2 专用（互不兼容） |

> **FLUX.1 的 LoRA 不兼容 FLUX.2**，SDXL LoRA 更不兼容。Pixel art 等风格需依赖 prompt 或训练 FLUX.2 专用 LoRA。

## 硬件需求

以 FLUX.2-dev bf16 全量为例：

| 组件 | VRAM 占用 |
|------|----------|
| DiT 主模型 | ~64 GB |
| Mistral Small 3.1 Text Encoder | ~18 GB |
| VAE | ~0.3 GB |
| **合计** | **~82 GB** |

H20（96GB HBM3）可放下全量 bf16，无需量化或 offload。

低显存方案：
- NF4 量化：~20GB
- NF4 + 远程 Text Encoder（HF 提供）：~18GB
- Group offloading：8GB GPU + 32GB RAM（极慢）

## 安装 ComfyUI

```bash
cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI

# 用 uv 创建虚拟环境（自动拉取 Python 3.13）
uv venv --python 3.13
source .venv/bin/activate

# H20 是 Hopper 架构，安装对应 CUDA 版本的 PyTorch
uv pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130

uv pip install -r requirements.txt
```

## 下载模型文件

### 前置：登录 Hugging Face

FLUX.2-dev 是 gated model，需要先在 [模型页面](https://huggingface.co/black-forest-labs/FLUX.2-dev) 同意 license：

```bash
uv pip install -U huggingface_hub
hf auth login
```

### 下载三个组件

```bash
# 1) DiT 主模型（64.4 GB，来自官方 repo）
hf download black-forest-labs/FLUX.2-dev flux2-dev.safetensors \
  --local-dir ~/ComfyUI/models/diffusion_models/

# 2) Text Encoder + VAE（来自 Comfy-Org 适配的单文件格式）
hf download Comfy-Org/flux2-dev \
  --include "split_files/text_encoders/mistral_3_small_flux2_bf16.safetensors" \
  --include "split_files/vae/flux2-vae.safetensors" \
  --local-dir /tmp/flux2-comfy-download/

# 移动到 ComfyUI 对应目录
mv /tmp/flux2-comfy-download/split_files/text_encoders/mistral_3_small_flux2_bf16.safetensors \
   ~/ComfyUI/models/text_encoders/

mv /tmp/flux2-comfy-download/split_files/vae/flux2-vae.safetensors \
   ~/ComfyUI/models/vae/
```

### 验证目录结构

```
ComfyUI/models/
  diffusion_models/
    flux2-dev.safetensors                        # 64.4 GB (bf16)
  text_encoders/
    mistral_3_small_flux2_bf16.safetensors       # ~18 GB (bf16)
  vae/
    flux2-vae.safetensors                        # ~336 MB
```

## 启动 ComfyUI

```bash
cd ~/ComfyUI
python main.py \
  --listen 0.0.0.0 \
  --port 8188 \
  --disable-auto-launch \
  --highvram
```

| 启动参数 | 说明 |
|---------|------|
| `--listen 0.0.0.0` | 允许远程访问 |
| `--port 8188` | 默认端口 |
| `--disable-auto-launch` | 不弹浏览器（服务器环境） |
| `--highvram` | 模型常驻显存，加速推理 |

访问 `http://<服务器IP>:8188` 打开 WebUI。

### 可选优化参数

```bash
python main.py \
  --listen 0.0.0.0 \
  --port 8188 \
  --disable-auto-launch \
  --highvram \
  --preview-method none
```

- `--preview-method none`：跳过预览生成，API 批量模式下节省时间

## 工作流搭建

FLUX.2 节点是 ComfyUI 内置的，不需要安装 custom nodes。

### 核心节点连接

```
UNETLoader ─model─> BasicGuider ─guider─> SamplerCustomAdvanced ──> VAEDecode ──> SaveImage
                        ^                      ^       ^                ^
CLIPLoader ─> CLIPTextEncode ─> FluxGuidance   │       │                │
              (prompt)          (guidance=4.0)  │       │                │
                                  │conditioning┘       │                │
                                                       │                │
EmptyFlux2LatentImage (1024x1024) ─latent──────────────┘                │
Flux2Scheduler (steps=50) ─sigmas──┘                                    │
KSamplerSelect (euler) ─sampler──┘                                      │
RandomNoise (seed) ─noise──┘                                            │
VAELoader ─vae──────────────────────────────────────────────────────────┘
```

### 节点配置

| 节点 | class_type | 关键参数 |
|------|-----------|---------|
| Load Diffusion Model | `UNETLoader` | unet_name: `flux2-dev.safetensors`, weight_dtype: `default` |
| Load Text Encoder | `CLIPLoader` | clip_name: `mistral_3_small_flux2_bf16.safetensors`, type: `flux2` |
| CLIP Text Encode | `CLIPTextEncode` | text: 你的 prompt（最多 512 tokens） |
| Flux Guidance | `FluxGuidance` | guidance: `4.0`（范围 2.5-5.0） |
| Empty Latent | `EmptyFlux2LatentImage` | width: `1024`, height: `1024` |
| Scheduler | `Flux2Scheduler` | steps: `50`（快速迭代用 `28`） |
| Sampler Select | `KSamplerSelect` | sampler_name: `euler` |
| VAE Loader | `VAELoader` | vae_name: `flux2-vae.safetensors` |

### 推荐参数速查

| 参数 | FLUX.2-dev | FLUX.2-klein |
|------|-----------|-------------|
| Steps | 50（或 28） | 4 |
| Guidance | 4.0 | 1.0 |
| Sampler | euler | euler |
| 分辨率 | 1024x1024 | 1024x1024 |
| Negative Prompt | 不支持 | 不支持 |

> **重要**：建议先在 WebUI 手动搭好工作流跑通，再通过 Settings > Dev Mode > "Save (API Format)" 导出 JSON，用作批量脚本的模板。

## API 批量生成

ComfyUI 的 API 是将工作流 JSON 整体 POST 到 `/prompt` 端点。

### 核心端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/prompt` | POST | 提交工作流到队列 |
| `/history/{prompt_id}` | GET | 查询执行结果 |
| `/view?filename=X&subfolder=Y&type=output` | GET | 下载生成的图片 |
| `/ws?clientId={uuid}` | WebSocket | 实时进度推送 |

### 批量生成脚本示例

```python
#!/usr/bin/env python3
"""FLUX.2-dev 批量生成（ComfyUI API）"""

import json, urllib.request, urllib.parse, time, os, uuid

SERVER = "127.0.0.1:8188"
OUTPUT_DIR = "./generated_sprites"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 从 ComfyUI "Save (API Format)" 导出的工作流 JSON
WORKFLOW_PATH = "flux2_workflow_api.json"

def queue_prompt(workflow):
    data = json.dumps({
        "prompt": workflow,
        "client_id": str(uuid.uuid4())
    }).encode()
    req = urllib.request.Request(
        f"http://{SERVER}/prompt", data=data,
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req).read())["prompt_id"]

def wait_and_download(prompt_id, output_path, poll_interval=2.0):
    while True:
        resp = json.loads(urllib.request.urlopen(
            f"http://{SERVER}/history/{prompt_id}").read())
        if prompt_id in resp:
            for node_out in resp[prompt_id]["outputs"].values():
                if "images" in node_out:
                    img = node_out["images"][0]
                    params = urllib.parse.urlencode({
                        "filename": img["filename"],
                        "subfolder": img["subfolder"],
                        "type": img["type"],
                    })
                    img_data = urllib.request.urlopen(
                        f"http://{SERVER}/view?{params}").read()
                    with open(output_path, "wb") as f:
                        f.write(img_data)
                    return
        time.sleep(poll_interval)

# 加载导出的工作流模板
with open(WORKFLOW_PATH) as f:
    base_workflow = json.load(f)

# 替换 prompt 节点的 text（节点 ID 以你导出的 JSON 为准）
PROMPT_NODE_ID = "3"

prompts = [
    "pixel art game sprite, centered single clownfish, side view, clean silhouette, limited 16 color palette, crisp pixel edges",
    "pixel art game sprite, centered single pufferfish, front view, round body, limited 12 color palette",
    # ...
]

for i, text in enumerate(prompts):
    wf = json.loads(json.dumps(base_workflow))
    wf[PROMPT_NODE_ID]["inputs"]["text"] = text
    pid = queue_prompt(wf)
    print(f"[{i+1}/{len(prompts)}] queued: {pid}")
    wait_and_download(pid, os.path.join(OUTPUT_DIR, f"sprite_{i:04d}.png"))
    print(f"  saved: sprite_{i:04d}.png")
```

### 使用步骤

1. 在 ComfyUI WebUI 中搭建并测试工作流
2. 开启 Dev Mode，点击 "Save (API Format)" 导出为 `flux2_workflow_api.json`
3. 确认导出 JSON 中 prompt 节点的 ID（修改脚本中的 `PROMPT_NODE_ID`）
4. 运行脚本

## 服务化部署（可选）

```ini
# /etc/systemd/system/comfyui.service
[Unit]
Description=ComfyUI Image Generation Server
After=network.target

[Service]
Type=simple
User=comfyui
WorkingDirectory=~/ComfyUI
ExecStart=~/ComfyUI/.venv/bin/python main.py \
  --listen 0.0.0.0 --port 8188 --disable-auto-launch --highvram
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now comfyui
```

## 常见问题

### 首次加载极慢

82GB 模型从磁盘读入显存需要时间，`--highvram` 确保后续推理时模型常驻不卸载。建议用 NVMe SSD 存放模型文件。

### 出图过饱和 / 失真

Guidance 过高。FLUX.2-dev 推荐 4.0，不要超过 5.0。

### CLIPLoader type 选项

在 GUI 下拉菜单中确认 type 值，应为 `flux2`。

### 想用 FLUX.2 做 pixel art

SDXL 和 FLUX.1 的 LoRA 都不兼容 FLUX.2。当前方案：
1. **纯 prompt**：FLUX.2 的 Mistral encoder 对风格描述理解很强，先试纯文本控制
2. **Flux2 专用 LoRA**：社区已开始训练，Civitai 搜索 "pixel art flux2"
3. **后处理**：出图后最近邻缩放 + 调色板量化

## 参考资料

- [FLUX.2-dev (Hugging Face)](https://huggingface.co/black-forest-labs/FLUX.2-dev)
- [Comfy-Org/flux2-dev 预处理权重](https://huggingface.co/Comfy-Org/flux2-dev)
- [ComfyUI GitHub](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI Flux 2 教程](https://docs.comfy.org/tutorials/flux/flux-2-dev)
- [HuggingFace Blog: FLUX.2](https://huggingface.co/blog/flux-2)
- [[Flux 部署指南 (Forge)|FLUX.1 部署指南 (Forge)]]
- [[Deployment MOC|部署推理]]
- [[Infra MOC|基础设施]]
