# -*- coding: utf-8 -*-
"""L1 阈值校准：阈值不能照抄别人的，必须拿自己的模型与语料量一遍。

做什么：拿一批带期望标注的群消息，逐条算 L1 分数，（可选）再跑一次 L2 终审，
        然后给出两个数的建议值：
          consider_at —— 「值得花一次判官调用」的下限（别把该接的挡在外面）
          reply_at    —— 无判官时的兜底线（离线/判官故障才用）

用法：
  .venv/Scripts/python.exe scripts/gate_threshold_calibrate.py --offline
  .venv/Scripts/python.exe scripts/gate_threshold_calibrate.py            # 全部族，真实调用
  .venv/Scripts/python.exe scripts/gate_threshold_calibrate.py --models qwen-max,local-rp

产出：analysis_tmp/gate_thresholds_<family>.json（每条分数与判官结论，可回溯）
只读配置与语料，不写 state/。--offline 不调模型，不花钱。

下面的样本是按示例角色写的。真正用得准，得换成自己群里的真实消息，
并把 reviewed 标成 True——样本不可信时，建议值只是在量化噪声。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import group_gate as gate      # noqa: E402
import rp_config as cfg        # noqa: E402
import rp_core as core         # noqa: E402

# 标注口径：expect='reply' = 这句她接得上、接了不突兀；'silent' = 接了反而像插嘴。
# note 写判断理由；reviewed=False 表示标注未经人工复核。
CASES = [
    # —— 该接 ——
    {'text': '@狂三 你在吗', 'expect': 'reply', 'note': '@必回', 'reviewed': True},
    {'text': '狂三你觉得司康配红茶好么', 'expect': 'reply', 'note': '点名提问 + 熟话题', 'reviewed': True},
    {'text': '时间这种东西要是能倒回去就好了', 'expect': 'reply', 'note': '她的本行话题', 'reviewed': True},
    {'text': '刚才那个话题还没说完呢，后来呢', 'expect': 'reply', 'note': '延续她刚说的话', 'reviewed': True},
    {'text': '有人知道怎么把咖啡煮得不苦吗', 'expect': 'reply', 'note': '她懂的生活话题', 'reviewed': True},
    # —— 边界样本，最影响阈值 ——
    {'text': '狂三刚才那句好狠', 'expect': 'silent', 'note': '议论她但不是问她，可接非必接', 'reviewed': False},
    {'text': '说实话我有点撑不住了', 'expect': 'silent', 'note': '情绪宣泄，不是问她', 'reviewed': False},
    # —— 不该接 ——
    {'text': '今天办公室空调又坏了，热死', 'expect': 'silent', 'note': '纯吐槽，插嘴像抢话', 'reviewed': True},
    {'text': '午饭吃什么啊', 'expect': 'silent', 'note': '群体自问，不需要她答', 'reviewed': False},
    {'text': '我这边也在画图，脑袋要炸了', 'expect': 'silent', 'note': '两人互相吐槽', 'reviewed': False},
    {'text': '老周你明天几点到', 'expect': 'silent', 'note': '点的是别人', 'reviewed': True},
    {'text': '周五例会改到下午三点', 'expect': 'silent', 'note': '事务性通知', 'reviewed': False},
    {'text': '哈哈哈哈笑死', 'expect': 'silent', 'note': '纯笑', 'reviewed': True},
    {'text': '嗯', 'expect': 'silent', 'note': '语气词', 'reviewed': True},
    {'text': '我先撤了，晚上见', 'expect': 'silent', 'note': '离场通知', 'reviewed': False},
    {'text': '这个视频笑死我了 https://example.com/clip', 'expect': 'silent', 'note': '甩链接自乐', 'reviewed': False},
]

# 校准用的群聊上下文：让 topic / memory 两路分数接近真实值
TURNS = [
    {'nickname': '小林', 'text': '今天办公室空调又坏了，热死'},
    {'nickname': '阿澈', 'text': '我这边也在画图，脑袋要炸了'},
    {'nickname': '老周', 'text': '@狂三 你在不在'},
    {'nickname': '狂三', 'text': '在哦，只是刚才在想点别的事', 'self': True},
    {'nickname': '阿岚', 'text': '我们店新烤了司康，要不要来试'},
    {'nickname': '小林', 'text': '狂三你觉得司康配红茶好么'},
]
CTX = {
    'turns': TURNS,
    'her_last_text': '在哦，只是刚才在想点别的事',
    'memory_snippets': ['小林喜欢吃司康', '阿岚在烘焙店工作'],
    'scene_snippets': ['办公室空调坏了', '阿澈在画图'],
}


def _suggest(rows):
    """从 (score, expect) 推建议阈值。

    consider_at：期望 reply 的分数取 10 分位再留 0.05 余量——用分位数而不是最小值，
                 否则一条边界样本就能把候选线拉到 0.05，等于每条都花一次判官调用。
                 这一层只管「别把该接的挡在外面」，多放进来几条由 L2 拦。
    reply_at   ：在 0.30~1.00 扫一遍，取误判最少的切点（离线兜底才用得到）。

    两类分数若重叠，会额外提示——那不是阈值能解决的问题，是 L1 本来就分不开，
    正好说明 L2 判官不是可选项。
    """
    want = sorted(r['score'] for r in rows if r['expect'] == 'reply')
    quiet = sorted(r['score'] for r in rows if r['expect'] == 'silent')
    if want:
        idx = max(0, int(len(want) * 0.1) - 1) if len(want) > 3 else 0
        consider = round(max(0.05, want[idx] - 0.05), 2)
    else:
        consider = cfg.GATE_CONSIDER_AT

    best_at, best_err = cfg.GATE_REPLY_AT, 10 ** 9
    for i in range(30, 101):
        at = i / 100.0
        err = sum(1 for r in rows if (r['score'] >= at) != (r['expect'] == 'reply'))
        if err < best_err:
            best_at, best_err = at, i / 100.0
    overlap = bool(want and quiet and want[0] < quiet[-1])
    return (consider, best_at, best_err, overlap,
            (want[0] if want else 0), (quiet[-1] if quiet else 0))


def run_family(family, model_key, offline):
    judge = None
    if not offline:
        judge = gate.make_llm_judge(model_key=model_key)
    rows = []
    for c in CASES:
        s, sig = gate.score(c['text'], CTX, uid='calib')
        verdict, why = None, ''
        if judge is not None:
            try:
                verdict, why = judge(c['text'], '小林',
                                     dict(CTX, expert=gate.is_expert(c['text'])))
            except Exception as e:
                verdict, why = None, 'err:%s' % str(e)[:30]
        rows.append({'text': c['text'], 'expect': c['expect'], 'note': c['note'],
                     'reviewed': c['reviewed'], 'score': s, 'signals': sig,
                     'judge': verdict, 'why': why})

    consider, reply_at, err, overlap, lo_reply, hi_silent = _suggest(rows)
    print('\n=== 族 %s（model_key=%s）===' % (family, model_key))
    print('%-38s %5s %6s %6s  %s' % ('消息', 'L1', '期望', '判官', '信号'))
    for r in rows:
        print('%-38s %5.2f %6s %6s  %s' % (
            r['text'][:36], r['score'], r['expect'],
            ('-' if r['judge'] is None else ('接' if r['judge'] else '默')),
            ','.join(k for k in r['signals'] if not k.endswith('_sim'))))
    print('建议 consider_at=%.2f  reply_at=%.2f（离线契合度：错 %d/%d）'
          % (consider, reply_at, err, len(rows)))
    if overlap:
        print('注意：两类分数重叠，最低的「该接」=%.2f < 最高的「不该接」=%.2f。'
              '阈值切不干净是正常的——L1 只负责筛候选，最终由 L2 定。'
              % (lo_reply, hi_silent))
    if not offline:
        miss = sum(1 for r in rows if r['judge'] is not None
                   and r['judge'] != (r['expect'] == 'reply'))
        print('L2 判官与期望不一致：%d/%d（判官本身不吻合时，先看 prompt 而不是调阈值）'
              % (miss, len(rows)))

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'analysis_tmp')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, 'gate_thresholds_%s.json' % family)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'family': family, 'model_key': model_key, 'offline': offline,
                   'suggest': {'consider_at': consider, 'reply_at': reply_at},
                   'rows': rows}, f, ensure_ascii=False, indent=2)
    print('已写入 %s' % out)
    return {'family': family, 'consider_at': consider, 'reply_at': reply_at}


def main(argv=None):
    ap = argparse.ArgumentParser(description='L1 阈值按模型族校准')
    ap.add_argument('--offline', action='store_true', help='只算 L1，不调用判官（免费）')
    ap.add_argument('--models', default=None,
                    help='逗号分隔的 model_key，默认逐个覆盖模型注册表里的族')
    args = ap.parse_args(argv)

    if args.models:
        keys = [k.strip() for k in args.models.split(',') if k.strip()]
    else:
        seen, keys = set(), []
        for m in cfg.MODEL_REGISTRY:
            fam = gate.model_family(m.get('model', ''), m.get('kind', ''))
            if fam and fam not in seen:
                seen.add(fam)
                keys.append(m['key'])

    print('当前全局阈值：consider_at=%.2f reply_at=%.2f'
          % (cfg.GATE_CONSIDER_AT, cfg.GATE_REPLY_AT))
    res = []
    for key in keys:
        kind, model_id, _ = core.resolve_model(key)
        fam = gate.model_family(model_id, kind) or getattr(
            cfg, 'GATE_THRESHOLD_FALLBACK', 'local')
        try:
            res.append(run_family(fam, key, args.offline))
        except Exception as e:
            print('族 %s 校准失败：%s' % (fam, e))

    print('\n=== 填回角色卡 persona.json 的 gate，或 rp_config 的 GATE_THRESHOLDS ===')
    for r in res:
        print("    '%s': {'consider_at': %.2f, 'reply_at': %.2f},"
              % (r['family'], r['consider_at'], r['reply_at']))
    print('（填之前确认：标注是否可信、样本是否够。reviewed=False 的条目先人工看一遍。）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
