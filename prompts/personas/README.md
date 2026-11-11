# 角色卡目录

本目录用于放运行时角色数据。为避免误提交真实角色，`.gitignore` 默认忽略
`prompts/personas/*`，只放行本文件、通用示例 `character/` 与同人示例
`kurumi/`。新建一个 `prompts/personas/<id>/` 目录，`<id>` 只能是小写字母、
数字、`_`、`-`。

`character/` 是一个可以直接运行的示例人格（名为「苏禾」），全部内容为
虚构占位，可复制成自己的角色后逐项替换：

```
prompts/personas/<id>/
  persona.json     角色注册信息
  card.json        固定设定、关系分档、背景条目、服装
  system_en.txt    由 card.json 渲染出的 system 文本
  stats.json       好感 / 信任 / 熟悉度的增减规则
  cg.json          可选：CG 身份层（发型、瞳色、分阶段服装）
  tts.json         可选：语音开关
```

`kurumi/` 是一份同人角色卡示例，角色为《约会大作战》的时崎狂三。它演示了
完整的 `gate` 权重、关系分档、背景条目与 `cg.json` 写法；角色及相关形象版权
归原作权利人所有，不适用本仓库的 MIT 许可。该目录没有 `tts.json`，所以语音
能力默认关闭。

## 必需文件

| 文件 | 作用 |
| --- | --- |
| `persona.json` | 角色注册信息：`id`、`label`、`display_name`、`aliases`、`assets`、`gate`、`qq_profile`、`group` |
| `card.json` | 固定设定（`static`）、关系分档（`relationship`）、背景条目（`lore`）、服装（`outfit`） |
| `system_en.txt` | 由 `card.json` 渲染出的 system 文本 |
| `stats.json` | 好感 / 信任 / 熟悉度的增减规则与档位说明 |

### persona.json

- `id` 必须与目录名一致。
- `label` / `display_name` 用于面板和称呼；`aliases` 用于 `/character` 解析。
- `assets` 指向本目录内的资产，缺文件时对应能力自动关闭。
- `gate.direct_names` 是群里判定「在叫这个角色」的名字；`gate.interest_words`
  与 `gate.expert_words` 用于直达判定加权，留空即可。
- `group.roster_limit` 控制群聊成员名册展开人数（默认 5）。

### card.json

`static` 至少包含 `Identity`、`Personality`、`Speech`、`Behavior`、`Rule`。
`relationship` 里 `levels` 必须与系统档位一致（`陌生/相识/熟络/亲密/倾心`），
`floor` 是各档最低数值。`lore.entries` 可以为空。

### stats.json

顶层 `active` 必须是 `characters` 中的一个 id。每个角色要写满三个数值
（`affection`、`trust`、`familiarity`）的 `gain_caps`、`gain_caps_big`、
`down_cap`；不写 `down_cap` 或写成负数会导致校验失败，运行时退化为中性
兜底（不涨也不扣，不会串用其他角色的规则）。

## 可选文件

| 文件 | 作用 |
| --- | --- |
| `cg.json` | CG 身份层。必填 `name_zh`、`city_zh`、`eye_lock_zh`、`identity_zh`、`identity_zh_back`、`back_focus_zh`、`back_subject_zh`；`outfits_zh` 按四个关系阶段给服装描述。`enabled: false` 可单独关闭该角色的 CG。 |
| `tts.json` | `{"enabled": true}` 表示允许这个角色发语音；缺省视为关闭。 |

CG 只读取人物设定与事件里的地点/心情，不发送聊天原话。发型、瞳色、服装只写
在 `cg.json`，共享模板里不放任何角色外观。

## 最小流程

1. 复制示例角色：`cp -r prompts/personas/character prompts/personas/<id>`，
   把 `persona.json` 和 `stats.json` 里的 id 全部改成新 id。
2. 写 `card.json`，`static` 下至少包含 `Identity`、`Personality`、`Speech`、
   `Behavior`、`Rule` 等键。
3. 生成 system 文本：

   ```bat
   .venv\Scripts\python.exe scripts\card_source.py --render --persona <id>
   ```

4. 在管理面板或 `runtime_config.character` 中切换到该角色。

## 注意

- 面板「角色卡」页直接编辑 `gate` 数值、触发动词、清单信息、英文角色文本与她的
  日常，保存时先备份再重新渲染并校验，任一步失败会回滚；页头的「导入角色卡」
  接收一份 JSON 包（粘贴或选文件，2 MB 以内），可覆盖同名角色并顺手切换过去。
- 除仓库自带的同人示例 `kurumi/` 外，不要把其他有版权的原作角色卡、语料、
  立绘或参考音频提交到公开仓库；确需入库时请自行修改 `.gitignore` 并确认授权。
- 卡片里不要写真实群友的 QQ 号、住址、职业等个人信息。
- 改完 `card.json` 后必须重新渲染，否则运行时仍读旧的 `system_en.txt`。
- `cg.json` 与 `stats.json` 都用 UTF-8；文件损坏只会影响该角色，不会回退到
  其他角色的设定。
