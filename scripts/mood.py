# -*- coding: utf-8 -*-
"""情绪短词表：照片与 CG 共用，演出端也只读这一份。

记忆卡里的情绪是自由文本（如「戏谑而略带羞涩」），先按词表归到最近的一档，
再各自展开成五官描述或表情标签。演出端只消费归并后的键，不直接读描述。
新增同义词只改这里的 MOOD_ALIASES，不要在两处各留一份。
"""

# 展开成眉眼、脸颊、嘴角与视线的具体画面。长身份段会把短描述稀释掉，
# 所以每条都写足部位和细节，且表情只留这一个来源。
MOOD_FACES = (
    ('大笑', '仰头大笑，嘴巴张开露出上排牙齿和一点舌尖，眼睛弯成月牙，'
             '眼角挤出细小的笑纹，脸颊鼓起来，肩膀跟着轻轻发颤'),
    ('忍笑', '紧紧抿住嘴唇，嘴角却压不住地往上翘，脸颊鼓鼓的，'
             '眼睛弯弯地憋着笑，鼻翼轻轻翕动'),
    ('害羞', '脸颊从颧骨一直红到耳根，头微微低下去，睫毛低垂，'
             '目光躲开镜头看向斜下方，嘴唇抿成一条柔软的线'),
    ('惊讶', '眼睛睁得圆圆的，眼白在上方露出一圈，眉毛高高抬起，'
             '嘴巴张开露出一点牙齿，头微微后仰，肩膀轻轻一缩'),
    ('专注', '目光牢牢锁在前方，眉心微微收紧，嘴唇轻轻抿住，呼吸放得很轻'),
    ('安静', '眉眼松弛，目光柔和地停着，嘴唇自然闭合，整个人安静下来'),
    ('温柔', '眼睛弯成温柔的月牙，眼角带着笑意，嘴角扬起浅浅的弧度，'
             '目光软软地落向镜头，脸颊透出一点淡淡的红'),
    ('温暖', '眼角先笑起来，嘴角跟着上扬，眼神柔和，脸颊有一层暖暖的颜色'),
    ('开心', '嘴角明显上扬，露出一点牙齿，眼睛弯起来，眉毛轻轻扬起'),
    ('好奇', '眼睛亮起来，头微微偏着，眉毛轻轻一挑，嘴唇微微张开'),
    ('放松', '眉眼舒展，嘴角自然放松，眼神平静，肩膀松松地垂着'),
    ('生气', '眉头紧紧皱起，眼睛微微眯起，嘴唇抿成一条向下压的线，下颌绷紧'),
    ('难过', '眉头向内垂下，眼眶微微发红，视线落向地面，嘴角轻轻向下撇'),
)

# 记忆卡里的情绪常是同义词或复合词，先归到词表里最近的一档。
# 顺序即优先级：复合词同时命中多条时，排在前面的生效。
MOOD_ALIASES = (
    ('羞涩', '害羞'), ('羞怯', '害羞'), ('脸红', '害羞'), ('宠溺', '温柔'),
    ('高兴', '开心'), ('愉快', '开心'), ('雀跃', '开心'), ('兴奋', '开心'),
    ('担心', '难过'), ('不安', '难过'), ('失落', '难过'), ('悲伤', '难过'),
    ('委屈', '难过'),
    ('恼怒', '生气'), ('懊恼', '生气'), ('不满', '生气'),
    ('震惊', '惊讶'), ('错愕', '惊讶'), ('吃惊', '惊讶'),
    ('平静', '放松'), ('安宁', '放松'), ('惬意', '放松'),
    ('戏谑', '忍笑'), ('促狭', '忍笑'),
)

# Anima 用同一套情绪归并，只把五官描述换成 Danbooru 表情标签。
MOOD_TAGS = (
    ('大笑', 'laughing, open mouth, teeth, closed eyes, happy'),
    ('忍笑', 'smile, closed mouth, suppressed smile, blush'),
    ('害羞', 'blush, embarrassed, looking away, half-closed eyes, shy'),
    ('惊讶', 'surprised, wide eyes, open mouth, raised eyebrows'),
    ('专注', 'serious, focused, narrowed eyes, closed mouth'),
    ('安静', 'calm, soft expression, closed mouth, serene'),
    ('温柔', 'gentle smile, soft smile, looking at viewer, half-closed eyes'),
    ('温暖', 'warm smile, blush, looking at viewer'),
    ('开心', 'happy, smile, open mouth, looking at viewer'),
    ('好奇', 'curious, head tilt, raised eyebrow, open mouth'),
    ('放松', 'relaxed, closed mouth, soft smile'),
    ('生气', 'angry, frown, narrowed eyes, pout'),
    ('难过', 'sad, frown, teary eyes, looking down'),
)

MOOD_KEYS = tuple(key for key, _ in MOOD_FACES)
_MOOD_FACE_MAP = dict(MOOD_FACES)
_MOOD_TAG_MAP = dict(MOOD_TAGS)


def mood_face(emotion):
    """短情绪落到具体五官：命中词表或同义词返回（词, 描述），否则空串。"""
    text = (emotion or '').strip()
    if not text:
        return '', ''
    for key, face in MOOD_FACES:
        if key in text:
            return key, face
    for word, key in MOOD_ALIASES:
        if word in text:
            return key, _MOOD_FACE_MAP[key]
    return '', ''


def mood_tags(emotion):
    """情绪归并后返回 Anima 表情标签；不在词表里返回空串。"""
    key, _face = mood_face(emotion)
    return _MOOD_TAG_MAP.get(key, '')


def classify(emotion):
    """归并后的情绪键；不在词表里返回空串。演出端只读这一层。"""
    return mood_face(emotion)[0]
