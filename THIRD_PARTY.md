# 第三方组件与素材

本仓库以 MIT 许可证发布自己的代码。下面这些组件**不随仓库分发**，需要使用者
自行安装、下载并遵守其各自的许可；本项目的代码只是在运行时通过公开 HTTP
接口或约定格式与它们交互，没有复制其源码。

## 运行时可选服务

| 组件 | 用途 | 交互方式 | 许可 |
| --- | --- | --- | --- |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) | 语音合成 | `POST /tts`（`api_v2.py`，JSON 入参，WAV 返回） | MIT |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | CG 生成 | `POST /prompt`、`GET /history/{id}`、`GET /view` | GPL-3.0 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | QQ 接入 | OneBot v11 反向 WebSocket | 见其仓库 |

- 上述项目由各自作者维护，本项目与其没有隶属或背书关系。
- 安装、升级与配置请以其官方文档为准；本仓库不提供它们的源码、安装包或补丁。
- 如果分发包含这些组件的整合包，需要同时满足其许可（尤其是 ComfyUI 的
  GPL-3.0）。

## 模型与权重

仓库不包含任何模型权重。示例工作流 `assets/cg/workflows/sd15_t2i.json` 默认
引用 `v1-5-pruned-emaonly.safetensors`，该文件需使用者自行下载并放入
ComfyUI 的 `models/checkpoints/`。

| 素材 | 说明 | 许可提示 |
| --- | --- | --- |
| SD1.5 checkpoint | 示例工作流的最小验证模型 | 通常为 CreativeML Open RAIL-M，使用前请阅读随模型附带的许可 |
| GPT-SoVITS 权重 | 语音合成所需 | 以官方发布页的许可与用途限制为准 |
| 参考音频 | 语音音色参考 | 必须是本人录制或已获授权的内容 |

## 角色卡与素材

- `prompts/personas/character/` 是虚构的通用示例，不含任何真实作品角色。
- `prompts/personas/kurumi/` 与 `assets/personas/kurumi/avatar.jpg` 是以
  《约会大作战》（作者：橘公司）的角色「时崎狂三」为原型的同人角色卡与素材，
  仅作角色卡格式示例。角色、名称、形象及相关设定的著作权归原作者与权利人
  所有；本项目与官方无隶属或背书关系，这些素材不适用仓库的 MIT 许可，也
  不应被用于商业用途。
- `assets/readme/` 下的头图是 AI 生成的插画，界面截图取自本机演示状态，
  里面的人名与数值都是虚构的，两者都不含官方素材。
- 其他真实角色卡、语料、立绘、CG 与参考音频默认被 `.gitignore` 忽略，由
  使用者自行准备并承担版权责任。
- 本项目不授予任何第三方角色、图像、语音或文本的版权许可。
