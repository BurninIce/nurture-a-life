# -*- coding: utf-8 -*-
"""手动 CG 生成（/cg）。

设计边界：
- **只由用户显式触发**：不按好感/信任阈值自动生成。阈值仍决定关系阶段，
  但要不要画、什么时候画，由用户发 `/cg` 决定。
- **画面锚定成员自己的事件**：读该成员最近一次共同经历（importance 高的优先），
  只取「地点 / 当时心情」两个可视字段，不把聊天原话、事件摘要发给绘图服务。
- **每次 /cg 都重新组提示词**：不缓存复用旧图。动作、机位、布光按成员与事件
  轮换，同一成员连续画也不会重复上一套提示词。
- **只走本地 ComfyUI**：不经过任何云端接口，也不需要公网。提示词、工作流、
  图片都留在本机。服务由用户自行安装（见 README「本地 CG」）。
- 生成图落盘到角色自己的运行态目录 state/personas/<id>/cg/<uid>/；
  同时写一份同名 .json 记录提示词/工作流/参数。
  CG 归属只认人物卡 uid：目录和 sidecar 的 uid 必须一致；
  群聊里发 /cg 也只会画发言者自己的图。
"""
import datetime
import hashlib
import json
import os
import sys
import threading
import time

import cg_profile
import comfy_client
import member_card
import runtime_config as rc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_OVERRIDE = None   # 测试用：只影响 cg_root()，不回写 persona 约定

PROMPT_VERSION = 1
EXTRA_MAX = 400


def _env_num(name, default, cast):
    try:
        return cast(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_size():
    raw = (os.environ.get('NAL_CG_SIZE') or '768x512').lower()
    try:
        w, h = raw.split('x', 1)
        return int(w), int(h)
    except (TypeError, ValueError):
        return 768, 512


CG_WIDTH, CG_HEIGHT = _env_size()
DEFAULT_STEPS = _env_num('NAL_CG_STEPS', 20, int)
DEFAULT_CFG = _env_num('NAL_CG_CFG', 7.0, float)
DEFAULT_NEGATIVE = (os.environ.get('NAL_CG_NEGATIVE')
                    or 'lowres, bad anatomy, extra fingers, watermark, text')

STAGES = ('相识', '熟络', '亲密', '倾心')
DEFAULT_STAGE = '相识'
STAGE_SLUG = {
    '相识': 'acquaintance',
    '熟络': 'familiar',
    '亲密': 'intimate',
    '倾心': 'devoted',
}

# 本模型的提示词是自然语言长段落（适合 SDXL / SD3 / Flux 一类模型）。
# 用 SD1.5 做最小验证也能出图，但画质不代表最终效果。
STYLE_BASE = (
    '整幅画追求galgame周年纪念CG / 视觉小说封面级的惊艳美学：'
    '以厚涂为主、辅以细腻线稿的日系数字绘画，笔触可见却干净，明暗过渡像油画一样柔和；'
    '皮肤有通透的次表面散射感，眼睛湿润有神、高光层次丰富，发丝一根根画出半透明边缘与柔亮光泽，'
    '衣料有真实的厚度、垂坠和柔软褶皱。'
)
STYLE_FACE = (
    '浅景深，人物从背景中轻轻浮出，脸和眼睛是绝对的视觉中心，'
    '%s构图讲究留白、视线引导与色彩层次，画面精致、浪漫、有故事感。'
)
STYLE_BODY = (
    '浅景深，人物轮廓与身形线条清晰，脸和眼睛保持可辨识的精致感，'
    '%s构图讲究留白、视线引导与色彩层次，画面精致、浪漫、有故事感。'
)
STYLE_BACK = (
    '浅景深，画面重点是%s，不出现正脸；'
    '构图讲究留白、视线引导与色彩层次，画面精致、浪漫、有故事感。'
)
STYLE_ENV = (
    '景深层次分明，人物与广阔环境形成大小对比，人物是画面的情绪锚点；'
    '构图讲究留白、视线引导与色彩层次，画面精致、浪漫、有故事感。'
)
STYLE_TAIL = (
    '允许适度的艺术夸张：线条精致、光影与色彩更浓烈，人体结构自然；'
    '整体是高分辨率、高细节、可印刷的插画质感。'
    '画布是连续完整的单一画幅，她整个人处在同一片光线与空间里，背景从她身后自然延伸。'
)

# 机位/景别/体态轮换：连续两次 /cg 一定换镜头。
# face=False 表示背影，不写表情与正脸；focus 决定画风句强调脸、身形还是环境。
SHOTS = (
    {'key': 'closeup', 'face': True, 'use_action': False, 'focus': 'face',
     'text': (
         '镜头是85毫米定焦、光圈f/1.4的极近特写，机位与她眼睛同高，只拍头部、颈部和肩线；'
         '她的脸占画面大半，双眼落在画面上部三分线上，睫毛、虹膜纹理和唇上的水光都清晰可见；'
         '她微微侧头，抬起眼直视镜头，几缕发丝被风带起、虚化着掠过脸颊；'
         '背景完全融化成两三种颜色的柔焦光斑，看不清环境细节。')},
    {'key': 'half', 'face': True, 'use_action': True, 'focus': 'face',
     'text': (
         '镜头是50毫米、光圈f/2的半身近景，机位与她胸口同高，拍到腰部以上；'
         '她身体侧转约四十五度，肩线与腰线形成优雅的斜线，脸转回来看向镜头；'
         '背景保留能辨认的环境轮廓，被浅景深柔化，前景有一片虚化的枝叶或光斑。')},
    {'key': 'full', 'face': True, 'use_action': True, 'focus': 'body',
     'text': (
         '镜头是35毫米、光圈f/2.8的全身像，机位与她腰部同高，从头到鞋底完整入画；'
         '她正面侧转约三十度站立，一只脚微微向前，重心落在后脚，站姿舒展自然；'
         '头顶与画面上边之间留出一点空间，鞋底与画面下边之间保留一小段地面；'
         '背景有两到三层景深，远处环境柔化成散景。')},
    {'key': 'side', 'face': True, 'use_action': False, 'focus': 'body',
     'text': (
         '镜头在她左侧、与她肩膀同高，用50毫米镜头跟拍她走路的一瞬间；'
         '她沿着面前的空间向前走，刚迈出一步、前脚落地，身体保持正侧面，脸望向前方，'
         '发梢和衣摆被脚步与风带向身后；'
         '这是运动中的一帧：她占据画面高度约三分之二，从头顶拍到膝盖上方，'
         '侧脸、肩线和腰胯的轮廓连成一条向前倾的线；'
         '背景随着她的前进向画面右侧流动，身后环境的线条连成一片连续的纵深；'
         '整张画面是一个连续不断、没有分割的单一空间，画面里只有她一个身影。')},
    {'key': 'back', 'face': False, 'use_action': False, 'focus': 'back',
     'text': (
         '镜头在她身后约两米，50毫米，拍一张背影全身像；'
         '她背对镜头，看不到正脸，重心落在一条腿上，肩线放松，一只手抬起拢住耳边的发丝；'
         '{back_subject}是画面主体，衣摆被风带起一点弧度；'
         '她面向画面前方的环境，背景向远处展开，形成她所注视的纵深。')},
    {'key': 'wide', 'face': True, 'use_action': True, 'focus': 'env',
     'text': (
         '镜头是18毫米超广角，机位低到膝盖高度：'
         '离镜头最近的栏杆或墙柱从画面左下角斜插进来，距离不到半米，被拉成一大片失焦的深色斜线；'
         '她站在十米外，人物在画面里很小，只占画面高度约四分之一；'
         '超广角透视把地面的砖缝、墙面的水平线和前景的栏杆猛烈地拉向远处的消失点，'
         '近处被放得极大、远处迅速缩小，画面左右边缘有轻微向外鼓起的透视拉伸；'
         '她站在画面右侧三分之一处，全身入镜，两只鞋的鞋底与地面严丝合缝，脚边落着一小片柔和的接触阴影；'
         '背景的空间层次层层退远，环境占据画面的大部分，她只是大空间里的一个焦点；'
         '画面是连续完整的单一空间，她位于画面中央，背景向远处展开。')},
    {'key': 'high', 'face': True, 'use_action': True, 'focus': 'body',
     'text': (
         '镜头从她斜上方约两米半处俯拍，35毫米，像从楼梯或高处的平台向下看；'
         '画面能看到她的头顶、肩背和脚下展开的地面，她微微仰起脸看向镜头；'
         '地面的纹理、被拉长的影子和周围环境在俯视下形成几何感很强的构图；'
         '她的身形约占画面一半，脸和眼睛仍然清晰。')},
    {'key': 'low', 'face': True, 'use_action': False, 'focus': 'body',
     'text': (
         '镜头贴近地面向上仰拍，24毫米广角，低角度带来强烈的透视；'
         '她坐在台阶或矮墙边缘，上身直立、微微前倾，膝盖并拢，双手交叠放在膝上；'
         '双腿自然垂下，双脚完整入镜，天空、屋檐或高处的结构向画面上方收束；'
         '她低下头看向镜头，发丝自然垂落，五官清晰可见，眼神冷静明亮。')},
)

# 光影与配色轮换：每套自带时间措辞，避免「午后」撞上「霓虹夜」。
LIGHTS = (
    {'key': 'golden', 'time': '深秋午后三点',
     'text': (
         '低角度的金色阳光从画面侧后方斜穿过来，勾出她的发丝、肩线和衣料边缘，'
         '空气里浮着细小的金色光尘，阴影是温暖的墨绿色；'
         '主色是琥珀金与墨绿，点缀一点暗红，色彩浓郁却不脏。')},
    {'key': 'blue', 'time': '日落后的蓝调时刻',
     'text': (
         '天空是深青蓝色，冷光笼罩整个环境，街灯与橱窗透出一点暖橘色，'
         '冷暖在人物轮廓上对撞；主色是青蓝与暖橘，白色高光收边，画面安静而高级。')},
    {'key': 'neon', 'time': '入夜后的雨夜',
     'text': (
         '绯红与青绿色的霓虹在湿润的空气里晕开，湿地面像镜子一样把灯光拉成长长的倒影，'
         '光斑落在她的发丝和肩上；主色是绯红、青绿与深黑，对比强烈，电影感十足。')},
    {'key': 'slats', 'time': '午后',
     'text': (
         '窗格或树叶切出的条纹光斜落在她身上，明暗像琴键一样交替，'
         '光柱里能看见缓慢漂浮的尘埃；主色是暖棕、金绿与奶白，温柔而有层次。')},
    {'key': 'spot', 'time': '夜晚',
     'text': (
         '一束冷白色的光从上方落下，像舞台聚光灯把她的轮廓从深黑背景里切出来，'
         '肩膀和发顶有锐利的高光，轮廓外缘有一圈冷白边光；'
         '背景仍保留隐约的城市灯光和酒红色暗部，不是纯黑；'
         '主色是冷白、深黑与酒红，戏剧张力很强。')},
    {'key': 'moon', 'time': '深夜',
     'text': (
         '冷白的月光从侧上方照下来，薄雾在空气里缓慢流动，远处的暖黄灯光变成小小的散景，'
         '发丝边缘泛着银蓝色的轮廓光；主色是银蓝、暖黄与雾灰，清冷又浪漫。')},
)

# 没有可画的事件地点时，按阶段退到这些默认地点（城市来自角色卡）。
DEFAULT_PLACES = {
    '相识': '{city}街角',
    '熟络': '{city}街角旧书店的过道',
    '亲密': '{city}河畔步道',
    '倾心': '{city}街角',
}
# 轮换用时间措辞，让同一成员连续 /cg 的提示词也不是同一套。
TIME_PHRASES = ('深秋午后两点', '深秋午后三点', '十一月初的午后一点半')

STAGE_EXPR = {
    '相识': '嘴角上扬，礼貌而好奇的浅笑，脸颊有淡红晕',
    '熟络': '眼睛弯起，明快的笑，脸颊有淡红晕',
    '亲密': '眉眼柔软，带一点羞意，脸颊明显泛红',
    '倾心': '笑容温暖而专注，眼睛里有光，脸颊有红晕',
}
# 每阶段三套安全动作（捧物/扶靠/坐姿）。按 uid 选一套，
# 让同阶段、同地点的不同成员也能拉开动作与站位。
ACTIONS = {
    '相识': (
        '她双手捧着一杯热饮停在胸前，微微侧头看向{target}。',
        '她单手扶在旁边的木栏杆上，身体轻轻侧靠，看向{target}。',
        '她双手拿着一小束花轻轻抱在身前，抬眼看向{target}。',
    ),
    '熟络': (
        '她双手捧着一本摊开的书停在胸前，读到一半抬起眼看向{target}。',
        '她单手扶在旁边的花架或墙面边缘，身体轻轻侧靠，看向{target}。',
        '她双手提着一只纸袋，肩背自然放松，侧头看向{target}。',
    ),
    '亲密': (
        '她侧身站着，单手轻扶旁边的石栏或墙面，头从肩侧转回来看向{target}。',
        '她双手捧着两杯热饮停在胸前，像正要递出一杯，看向{target}。',
        '她坐在旁边的矮墙或台阶上，正面朝向镜头，双腿并拢自然垂下、双脚平踩在地面，'
        '上身微微前倾，双手交叠放在膝上，头抬起来看向{target}。',
    ),
    '倾心': (
        '她坐在旁边的矮墙或台阶上，正面朝向镜头，双腿并拢自然垂下、双脚平踩在地面，'
        '上身微微前倾，双手交叠放在膝上，头抬起来专注地看向{target}。',
        '她单手拢着被风轻轻吹起的围巾，侧身回眸看向{target}。',
        '她双手捧着一小束花抱在身前，唇角上扬，看向{target}。',
    ),
}

_LOCK = threading.RLock()
_BUSY = False
_LOCAL_LOCK = threading.Lock()
_LEDGERS = {}   # CG 根目录 -> 账本，按角色分开


class CgError(RuntimeError):
    pass


# ---------- 提示词 ----------

def normalize_stage(stage):
    return stage if stage in STAGES else DEFAULT_STAGE


# 这些场景只是聊天来源，不是能画出来的地点。
_NON_VISUAL_SCENES = frozenset(('', '群聊', '私聊', '聊天', '未知'))
# 只在最近几条里找高重要度事件；更早的「重要事件」不代表此刻的场景。
ANCHOR_WINDOW = 8


def anchor_for(uid):
    """该成员最近一次可作画面锚点的共同经历。

    最近几条里优先取「私聊里的重要事件」，其次重要事件，再次私聊事件，
    最后退到最近一条带地点或心情的事件。只回传地点/心情/标签；
    事件摘要与聊天原话都不取，避免把隐私内容发给绘图服务。
    """
    try:
        import rp_core
        data = rp_core.snapshot(uid)
    except Exception:
        return None
    eps = [e for e in (data.get('episodes') or [])
           if isinstance(e, dict) and not e.get('inactive')]
    if not eps:
        return None

    def usable(e):
        scene = (e.get('scene') or '').strip()
        return scene not in _NON_VISUAL_SCENES or bool((e.get('emotion') or '').strip())

    recent = eps[-ANCHOR_WINDOW:]

    def important(e):
        try:
            return int(e.get('importance') or 1) >= 2
        except (TypeError, ValueError):
            return False

    def private(e):
        return e.get('channel') != 'group'

    def pick(pred):
        return next((e for e in reversed(recent) if pred(e)), None)

    # 私聊里的重要时刻优先——群聊事件多是打趣，不适合当亲密 CG 的锚点。
    picked = (pick(lambda e: important(e) and private(e) and usable(e))
              or pick(lambda e: important(e) and usable(e))
              or pick(lambda e: private(e) and usable(e))
              or pick(usable))
    if picked is None:
        return None
    scene = (picked.get('scene') or '').strip()
    return {
        'id': picked.get('id'),
        'time': picked.get('time') or '',
        'scene': '' if scene in _NON_VISUAL_SCENES else scene[:20],
        'emotion': (picked.get('emotion') or '').strip()[:12],
        'tags': [str(t)[:8] for t in (picked.get('tags') or [])][:3],
    }


def _variant(uid, anchor):
    seed = '%s|%s' % (uid or '', (anchor or {}).get('id') or '')
    return int(hashlib.sha1(seed.encode('utf-8')).hexdigest(), 16)


def _shot(variant):
    return SHOTS[variant % len(SHOTS)]


def _light(variant):
    # 步长 5 与 6 互质：连续生成时光影也一定会换，不会连着八张同一套布光。
    return LIGHTS[(variant * 5) % len(LIGHTS)]


def _style(shot, profile):
    focus = shot.get('focus', 'face')
    line = {
        'face': STYLE_FACE % profile['eye_lock_zh'],
        'body': STYLE_BODY % profile['eye_lock_zh'],
        'back': STYLE_BACK % profile['back_focus_zh'],
        'env': STYLE_ENV,
    }[focus]
    return STYLE_BASE + line + STYLE_TAIL


def _place(profile, scene):
    city = profile['city_zh']
    return scene if scene.startswith(city) else city + '的' + scene


def _extra_clause(extra):
    """把 /cg <内容> 的用户补充要求收成一句，拼在取景句之后、画风句之前。"""
    text = ' '.join((extra or '').split())
    if not text:
        return ''
    if len(text) > EXTRA_MAX:
        text = text[:EXTRA_MAX].rstrip() + '…'
    if text[-1] not in '。！？；.!?;':
        text += '。'
    return '额外画面要求：' + text


def build_prompt(stage, anchor=None, uid='', variant=None, extra='', card=None):
    """按关系阶段、成员事件与轮换序号拼纯文生图提示词。

    顺序：身份→动作→服装→时间地点→神情→机位→光色→风格。
    """
    profile = cg_profile.load(card)
    stage = normalize_stage(stage)
    anchor = anchor or {}
    scene = ((anchor.get('scene') or '').strip()
             or DEFAULT_PLACES[stage].format(city=profile['city_zh']))
    place = _place(profile, scene)
    emotion = (anchor.get('emotion') or '').strip()
    if variant is None:
        variant = _variant(uid, anchor)
    shot = _shot(variant)
    light = _light(variant)
    actions = ACTIONS[stage]
    action = actions[variant % len(actions)].format(target='镜头')
    expr = STAGE_EXPR[stage]
    if emotion:
        expr = '%s，眉眼间还留着当时%s的感觉' % (expr, emotion)
    parts = [profile['identity_zh_back'] if not shot['face']
             else profile['identity_zh']]
    if shot['use_action']:
        parts.append(action)
    if shot['key'] != 'closeup':
        parts.append('她穿着%s' % profile['outfits_zh'][stage])
    parts.append('%s，%s。' % (light['time'], place))
    if shot['face']:
        parts.append('她的神情是%s。' % expr)
    parts.extend((shot['text'].replace('{back_subject}',
                                       profile['back_subject_zh']),
                  light['text'], _extra_clause(extra), _style(shot, profile)))
    return ''.join(parts)


# ---------- 关系阶段 ----------

def stage_for(uid):
    """当前关系阶段；没有记忆卡时按「相识」。"""
    try:
        import rp_core
        data = rp_core.snapshot(uid)
        rel = (data.get('card') or {}).get('relationship')
        return normalize_stage(rel)
    except Exception:
        return DEFAULT_STAGE


def owner_name(uid):
    try:
        return (member_card.load(uid).get('nickname') or '').strip()
    except Exception:
        return ''


# 8 套机位 × 6 套光影 × 3 套动作（站位与时间随光色轮换），联合周期 72。
VARIANT_CYCLE = 72
_SCAN_META_CACHE = {}


def cg_root(pid=None):
    """角色自己的 CG 运行态根目录（state/personas/<id>/cg）。测试覆盖优先。"""
    if _STATE_OVERRIDE:
        return _STATE_OVERRIDE
    import persona
    return os.path.join(persona.state_root(pid), 'cg')


def _last_variant(uid, out_root=None):
    """上一次生成用的轮换序号；只看当前角色的记录，没有时返回 None。"""
    uid = str(uid or '').strip()
    if not uid:
        return None
    root = out_root or cg_root()
    best_ts, best = -1.0, None
    for folder in (os.path.join(root, uid), os.path.join(root, '_meta', uid)):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith('.json'):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding='utf-8') as f:
                    meta = json.load(f)
                variant = int(meta.get('variant'))
                ts = os.path.getmtime(path)
            except (OSError, ValueError, TypeError):
                continue
            if ts > best_ts:
                best_ts, best = ts, variant
    return best


def _next_variant(uid, anchor, out_root=None):
    """下一个轮换序号；相邻两次的动作或站位一定不同。"""
    last = _last_variant(uid, out_root)
    if last is None:
        return _variant(uid, anchor) % VARIANT_CYCLE
    return (last + 1) % VARIANT_CYCLE


# ---------- 限额 ----------

def set_state_root(path=None):
    """测试用：覆盖生成记录根目录；传 None 恢复正常按角色分目录。"""
    global _STATE_OVERRIDE
    _STATE_OVERRIDE = path or None
    _LEDGERS.clear()


def _ledger_path(root=None):
    return os.path.join(root or cg_root(), 'ledger.json')


def _load_ledger(root=None):
    root = root or cg_root()
    if root in _LEDGERS:
        return _LEDGERS[root]
    try:
        with open(_ledger_path(root), encoding='utf-8') as f:
            data = json.load(f)
        _LEDGERS[root] = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _LEDGERS[root] = {}
    return _LEDGERS[root]


def _save_ledger(root=None):
    root = root or cg_root()
    data = _load_ledger(root)
    os.makedirs(root, exist_ok=True)
    tmp = _ledger_path(root) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _ledger_path(root))


def _prune(rows, now=None):
    """只保留最近两天，够判断「今日次数」与冷却。"""
    now = now or time.time()
    return [float(t) for t in rows if now - float(t) < 2 * 86400]


def _today_count(rows, now=None):
    now = now or time.time()
    today = datetime.date.fromtimestamp(now)
    n = 0
    for t in rows:
        if datetime.date.fromtimestamp(float(t)) == today:
            n += 1
    return n


def claim(uid, force=False, card=None, pid=None):
    """申请生成额度。成功后必须 release()（generate 内部会自动释放）。

    force=True 跳过冷却与每日上限，且不消耗额度，仅供管理员 /cg new 测试用；
    忙碌锁和本地客户端可用性仍然要检查。card 传当前角色的卡，避免校验错角色；
    pid 决定记在哪个角色的账本上。
    返回 (ok, reason, meta)。meta 含 stage/count，供调用方展示。
    """
    global _BUSY
    root = cg_root(pid)
    with _LOCK:
        if not rc.get('cg_enabled', True):
            return False, 'CG 生成现在是关着的。', {}
        if _BUSY:
            return False, '还在画上一张，等一会儿再试。', {}
        if _LOCAL_LOCK.locked():
            return False, '还在画上一张，等一会儿再试。', {}
        try:
            profile = cg_profile.load(card)
        except (OSError, ValueError):
            return False, '角色卡的 CG 段读不到，暂时画不了。', {}
        info = comfy_client.describe()
        if not (info.get('workflow') or {}).get('exists'):
            return False, 'ComfyUI 工作流不在本地（%s）。' % (
                (info.get('workflow') or {}).get('path') or '未配置'), {}

        now = time.time()
        rows = _prune(_load_ledger(root).get(str(uid), []), now)
        cooldown = int(rc.get('cg_cooldown', 600) or 0)
        if not force and rows and cooldown > 0:
            left = cooldown - (now - max(rows))
            if left > 0:
                return False, '刚画过一张，%d 分钟后再试。' % max(1, int(left // 60 + 0.999)), {}
        daily_max = int(rc.get('cg_daily_max', 5) or 0)
        if not force and daily_max > 0 and _today_count(rows, now) >= daily_max:
            return False, '今天已经画了 %d 张，明天再画吧。' % daily_max, {}

        if not force:
            rows.append(now)
            _load_ledger(root)[str(uid)] = rows
            try:
                _save_ledger(root)
            except OSError:
                return False, '生成记录写不进去，暂时画不了。', {}
        _BUSY = True
        return True, '', {'stage': stage_for(uid), 'count': len(rows)}


def release():
    global _BUSY
    with _LOCK:
        _BUSY = False


def reset_for_test():
    """测试用：清掉进程内的忙碌标记与缓存账本。"""
    global _BUSY
    with _LOCK:
        _BUSY = False
        _LEDGERS.clear()


# ---------- 生成 ----------

def _seed_for():
    return int(time.time() * 1000) % 2147483647


def generate(uid, stage=None, prompt=None, width=None, height=None, steps=None,
             cfg=None, seed=None, out_root=None, anchor=None, timeout=300,
             extra='', card=None, pid=None):
    """用本地 ComfyUI 画一张。

    prompt 传了就原样用；不传则按关系阶段与事件组纯文生图提示词，
    extra 会作为额外画面要求追加在取景句之后。
    调用前应先用 claim() 申请额度；这里在 finally 里 release()。
    """
    root = out_root or cg_root(pid)
    try:
        stage = normalize_stage(stage or stage_for(uid))
    except Exception:
        stage = DEFAULT_STAGE
    if not _LOCAL_LOCK.acquire(blocking=False):
        return {'ok': False, 'error': '上一张还在画，等它出来再试。',
                'stage': stage}
    try:
        profile = cg_profile.load(card)
        if anchor is None:
            anchor = anchor_for(uid)
        try:
            seed = int(seed) if seed not in (None, '') else None
        except (TypeError, ValueError):
            raise CgError('seed 必须是整数')
        variant = _next_variant(uid, anchor, root)
        custom = bool((prompt or '').strip())
        text = (prompt or '').strip() if custom else build_prompt(
            stage, anchor, uid=uid, variant=variant, extra=extra, card=card)
        source = 'custom' if custom else ('auto+extra' if extra else 'auto')
        got = comfy_client.generate(
            text, width=width or CG_WIDTH, height=height or CG_HEIGHT,
            steps=steps or DEFAULT_STEPS, cfg=DEFAULT_CFG if cfg is None else cfg,
            seed=seed, negative=DEFAULT_NEGATIVE, timeout=timeout)
        out_dir = os.path.join(root, str(uid))
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        name = '%s_%s_%d.png' % (stamp, STAGE_SLUG.get(stage, 'cg'),
                                 got['seed'])
        out = os.path.join(out_dir, name)
        base, ext = os.path.splitext(out)
        n = 1
        while os.path.exists(out):
            out = '%s_%d%s' % (base, n, ext)
            n += 1
        with open(out, 'wb') as f:
            f.write(got['png'])
        with open(out, 'rb') as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        meta = {
            'uid': str(uid),
            'owner_name': owner_name(uid),
            'engine': 'comfyui',
            'card': os.path.relpath(profile['path'], ROOT).replace(os.sep, '/'),
            'stage': stage,
            'prompt_version': PROMPT_VERSION,
            'anchor_id': anchor.get('id') if anchor else None,
            'anchor': anchor,
            'variant': variant,
            'prompt_source': source,
            'workflow': os.path.basename(comfy_client.workflow_path()),
            'width': got['width'],
            'height': got['height'],
            'steps': steps or DEFAULT_STEPS,
            'cfg': DEFAULT_CFG if cfg is None else cfg,
            'seed': got['seed'],
            'prompt': text,
            'file': os.path.relpath(out, ROOT).replace(os.sep, '/'),
            'sha256': digest,
            'elapsed_ms': got['elapsed_ms'],
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'egress': '提示词只发到本机 ComfyUI，不经过公网',
        }
        with open(os.path.splitext(out)[0] + '.json', 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return {'ok': True, 'name': os.path.basename(out), 'path': out,
                'stage': stage, 'seed': got['seed'],
                'width': got['width'], 'height': got['height'],
                'elapsed_ms': got['elapsed_ms'], 'bytes': len(got['png']),
                'prompt': text, 'prompt_source': meta['prompt_source'],
                'meta': meta}
    except Exception as e:
        return {'ok': False, 'error': short_error(str(e)), 'stage': stage}
    finally:
        _LOCAL_LOCK.release()
        release()


def preview(uid, stage=None, card=None, pid=None):
    """面板预览用：和 generate 同一套提示词，但不生成、不写记录。"""
    stage = normalize_stage(stage or stage_for(uid))
    anchor = anchor_for(uid)
    variant = _next_variant(uid, anchor, cg_root(pid))
    return {'stage': stage, 'variant': variant, 'anchor': anchor,
            'prompt': build_prompt(stage, anchor, uid=uid,
                                   variant=variant, card=card)}


def short_error(text):
    t = (text or '未知原因').strip()
    return t[:80]


def selftest():
    """纯离线自测：提示词拼装、轮换、事件锚点与工作流填充。"""
    import shutil
    import tempfile
    ok = True
    tmp = tempfile.mkdtemp(prefix='cg_selftest_')
    try:
        card_dir = os.path.join(tmp, 'personas', 'demo')
        os.makedirs(card_dir)
        card_path = os.path.join(card_dir, 'card.json')
        with open(card_path, 'w', encoding='utf-8') as f:
            json.dump({'character': 'Demo', 'cg': {}}, f)
        with open(os.path.join(card_dir, 'cg.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'name_zh': '苏禾',
                'city_zh': '示例市',
                'eye_lock_zh': '琥珀色瞳孔',
                'identity_zh': '一位二十岁出头的年轻女性，黑色直发，琥珀色眼睛。',
                'identity_zh_back': '一位二十岁出头的年轻女性，黑色长发垂到腰间。',
                'back_focus_zh': '垂到腰间的黑色长发',
                'back_subject_zh': '被风轻轻带起的发梢',
                'outfits_zh': {s: '米色针织衫和深色长裙' for s in STAGES},
            }, f, ensure_ascii=False)

        anchor_k = {'id': 7, 'scene': '花店门口', 'emotion': '安静'}
        shots = [build_prompt('熟络', anchor_k, uid='1001', variant=v,
                              card=card_path) for v in range(8)]
        ok = ok and len(set(shots)) == 8
        for phrase in ('极近特写', '半身近景', '全身像', '跟拍她走路',
                       '背影全身像', '18毫米超广角', '俯拍', '仰拍'):
            ok = ok and any(phrase in p for p in shots)
        back = shots[4]
        ok = ok and '垂到腰间的黑色长发' in back
        ok = ok and '她的神情是' not in back
        ok = ok and '琥珀色瞳孔' not in back
        default_place = build_prompt('相识', None, uid='1001', variant=0,
                                     card=card_path)
        ok = ok and '示例市街角' in default_place

        lights = [build_prompt('熟络', anchor_k, uid='1001', variant=v,
                               card=card_path) for v in range(6)]
        for phrase in ('金色光尘', '蓝调时刻', '湿地面像镜子', '条纹光',
                       '舞台聚光灯', '薄雾在空气里缓慢流动'):
            ok = ok and any(phrase in p for p in lights)

        xp = build_prompt('熟络', {'id': 7, 'scene': '花店门口'}, uid='1001',
                          variant=0, extra='  她回头  笑一下  ',
                          card=card_path)
        ok = ok and '额外画面要求：她回头 笑一下。' in xp
        ok = ok and '额外画面要求' not in build_prompt(
            '熟络', None, uid='1001', variant=0, card=card_path)
        long_extra = '很长' * 300
        lp = build_prompt('熟络', None, uid='1001', variant=0,
                          extra=long_extra, card=card_path)
        ok = ok and ('额外画面要求：' + long_extra[:EXTRA_MAX].rstrip() + '…') in lp

        variants = {build_prompt('相识', {'id': 3, 'scene': '示例市街角'},
                                 uid=u, card=card_path)
                    for u in ('1001', '1002', '1003', '1004')}
        ok = ok and len(variants) > 1
        seq = [build_prompt('熟络', anchor_k, uid='1001', variant=i,
                            card=card_path) for i in range(6)]
        ok = ok and len(set(seq)) == 6
        ok = ok and all(seq[i] != seq[i + 1] for i in range(5))

        import rp_core
        orig_snapshot = rp_core.snapshot
        try:
            rp_core.snapshot = lambda uid=None: {'episodes': [
                {'id': 1, 'importance': 1, 'channel': 'group',
                 'scene': '群聊', 'emotion': ''},
                {'id': 2, 'importance': 2, 'channel': 'private',
                 'scene': '示例市街角', 'emotion': '温柔'},
                {'id': 3, 'importance': 3, 'channel': 'group',
                 'scene': '花店门口', 'emotion': '戏谑', 'tags': ['回忆']},
                {'id': 4, 'importance': 1, 'channel': 'private',
                 'scene': '河畔步道', 'emotion': '平静'},
            ]}
            got = anchor_for('x')
            ok = ok and got and got['id'] == 2 and got['scene'] == '示例市街角'
            rp_core.snapshot = lambda uid=None: {'episodes': []}
            ok = ok and anchor_for('x') is None
        finally:
            rp_core.snapshot = orig_snapshot

        set_state_root(None)
        ok = ok and os.path.normcase(cg_root('character')) == os.path.normcase(
            os.path.join(ROOT, 'state', 'personas', 'character', 'cg'))
        ok = ok and os.path.normcase(cg_root('alt')) == os.path.normcase(
            os.path.join(ROOT, 'state', 'personas', 'alt', 'cg'))
        ok = ok and cg_root('character') != cg_root('alt')

        state = os.path.join(tmp, 'state')
        set_state_root(state)
        folder = os.path.join(state, '1001')
        os.makedirs(folder)
        base = _variant('1001', anchor_k) % VARIANT_CYCLE
        ok = ok and _last_variant('1001') is None
        ok = ok and _next_variant('1001', anchor_k) == base
        with open(os.path.join(folder, 'x.json'), 'w', encoding='utf-8') as f:
            json.dump({'uid': '1001', 'variant': 4}, f)
        ok = ok and _last_variant('1001') == 4
        ok = ok and _next_variant('1001', anchor_k) == 5
        with open(os.path.join(folder, 'legacy.json'), 'w', encoding='utf-8') as f:
            json.dump({'uid': '1001'}, f)
        ok = ok and _last_variant('1001') == 4
    finally:
        set_state_root(None)
        reset_for_test()
        shutil.rmtree(tmp, ignore_errors=True)

    # ComfyUI 工作流：按连线找节点并填参数，不连服务。
    wf = {
        '4': {'class_type': 'CheckpointLoaderSimple',
              'inputs': {'ckpt_name': 'demo.safetensors'}},
        '3': {'class_type': 'KSampler',
              'inputs': {'seed': 0, 'steps': 20, 'cfg': 7.0,
                         'sampler_name': 'euler', 'scheduler': 'normal',
                         'denoise': 1.0,
                         'model': ['4', 0], 'positive': ['6', 0],
                         'negative': ['7', 0], 'latent_image': ['5', 0]}},
        '5': {'class_type': 'EmptyLatentImage',
              'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
        '6': {'class_type': 'CLIPTextEncode',
              'inputs': {'text': '', 'clip': ['4', 1]}},
        '7': {'class_type': 'CLIPTextEncode',
              'inputs': {'text': '', 'clip': ['4', 1]}},
    }
    filled, seed = comfy_client.compose(wf, '测试提示词', width=768, height=512,
                                        steps=8, cfg=1.0, seed=42)
    ok = ok and seed == 42
    ok = ok and filled['6']['inputs']['text'] == '测试提示词'
    ok = ok and filled['5']['inputs']['width'] == 768
    ok = ok and filled['3']['inputs']['steps'] == 8
    ok = ok and wf['6']['inputs']['text'] == ''   # 不改模板
    try:
        comfy_client.compose({'1': {'class_type': 'SaveImage', 'inputs': {}}},
                             'x')
        ok = False
    except comfy_client.ComfyError:
        pass

    print('cg_gen selftest:', 'ok' if ok else 'FAIL')
    return ok


if __name__ == '__main__':
    sys.exit(0 if selftest() else 1)
