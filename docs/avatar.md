# 形象与语音的接法

这个仓库只负责后端的一部分，角色形象与声音都不随仓库分发。这里写清楚两条链路怎么接，以及能直接用哪些公开模型。

## 静态头像与立绘

`prompts/personas/<id>/` 里有两处与外观有关。

`persona.json` 的 `qq_profile.avatar` 指向一张方形 jpg，同步到 QQ 资料卡时用它。`cg.json` 的 `identity_zh` 是出图时喂给模型的完整外形描述，`outfits_zh` 是按关系档位换的衣服，`eye_lock_zh` 锁住瞳色不漂移。三处写一致，出图才会像同一个人。

示例卡的头像与 CG 都是文生图出来的，仓库里没有美术素材。换成自己的角色时，按上面三处改写，再拿任意本地或云端文生图模型出图即可。

## Live2D

仓库带一个本地展示台，`scripts/live2d_stage.py`。它起一个只在本机监听的小服务，页面上渲染一个 Live2D 模型，情绪由 `/mood` 接口推进来。

```bat
.venv\Scripts\python.exe scripts\live2d_stage.py --port 6798
```

打开 `http://127.0.0.1:6798/` 就能看到模型。换模型用 `?model=<模型 json 的 URL>`，默认指向 pixi-live2d-display 仓库里的公开示例（Haru）。给情绪的方式：

```bat
curl -X POST http://127.0.0.1:6798/mood -H "Content-Type: application/json" -d "{\"mood\": \"戏谑而略带羞涩\"}"
```

情绪先按 `scripts/mood.py` 的十三档词表归并，演出端只读归并后的键。模型没有对应表情时停在上一个，不做随机。

页面里的 pixi、pixi-live2d-display 与 Cubism Core 都从 CDN 取，所以浏览器要联网。Cubism Core 由 Live2D 官方提供，商用时先确认其许可；想彻底避开这条依赖，可以换 [Inochi2D](https://github.com/Inochi2D/inochi2d) 这类开源运行时，接口约定不变，仍然是往 `/mood` 推情绪键。

想接完整的对话前端，可以再用 [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)。它用一个本地服务把 Live2D 前端和对话后端连起来，模型目录、人格提示词、TTS 都在它的配置里。

模型用公开示例，不要用商业作品的角色立绘。可用的几套如下，下载后放进 Open-LLM-VTuber 的 `live2d-models/` 目录，再在 `conf.yaml` 里按它的格式登记。

- Live2D 官方的示例模型，包括 Hiyori、Haru、Mao 等。Live2D Inc. 提供了示例模型下载，个人非商业使用免费，商用要单独授权。许可条款见 Live2D 官网的示例模型页面。
- VTube Studio 自带的示例模型。许可随 VTube Studio 走，只能在其生态内使用。
- Inochi2D 项目的示例模型。开源项目自带的模型多为宽松许可，具体以仓库里的 LICENSE 为准。

`conf.yaml` 里与模型有关的两段大致如下，路径换成你自己的目录即可。

```yaml
live2d_model_name: hiyori
model_dict_path: live2d-models/model_dict.json
```

角色设定那边不要另写一份人格。把本项目 `prompts/personas/toki/system_en.txt` 的内容作为系统提示词贴进去，记忆与关系数值仍由本项目维护，Live2D 只当显示层。

## 语音

TTS 走 GPT-SoVITS，面板「语音」页可以填服务地址与参考音频。仓库不带声线，也不带训练好的模型。

公开可用的中文声线可以从 GPT-SoVITS 官方示例与开源语音数据集里找，注意每条音频的授权范围；用他人声音做克隆前先确认许可，商用场景尤其要谨慎。

配置好后，`/tts` 指令和回复里的语音触发都会走这条链路，合成失败只回一句提示，不影响文字。

## 许可提醒

上面提到的模型与声线都不在本仓库的 MIT 许可范围内，使用前请逐个确认其许可条款。仓库自带的示例角色「时之少女」与「苏禾」是原创设定，名字、外形与过去都不指向任何既有作品。
