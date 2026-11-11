# -*- coding: utf-8 -*-
"""本地 ComfyUI 客户端（只用标准库）。

对端是用户自己启动的 ComfyUI（独立项目，GPL-3.0），本仓库不包含它，
只通过公开 HTTP API 调用：

    POST /prompt          提交 API 格式工作流
    GET  /history/{id}    轮询结果
    GET  /view            取回图片

配置：
    NAL_COMFY_URL        服务地址，默认 http://127.0.0.1:8188
    NAL_COMFY_WORKFLOW   工作流 JSON，默认 assets/cg/workflows/sd15_t2i.json

工作流要求：必须能通过连线找到 KSampler/KSamplerAdvanced、正/反向
CLIPTextEncode 和 EmptyLatentImage/EmptySD3LatentImage。示例见
assets/cg/workflows/sd15_t2i.json。
"""
import copy
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = 'http://127.0.0.1:8188'
DEFAULT_WORKFLOW = os.path.join(ROOT, 'assets', 'cg', 'workflows', 'sd15_t2i.json')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

LATENT_TYPES = ('EmptyLatentImage', 'EmptySD3LatentImage',
                'EmptyLatentImageSDXL')


class ComfyError(Exception):
    pass


def _short(text, limit=200):
    text = str(text or '').replace('\n', ' ').strip()
    return text[:limit]


def base_url():
    return (os.environ.get('NAL_COMFY_URL') or DEFAULT_URL).strip().rstrip('/')


def workflow_path():
    return (os.environ.get('NAL_COMFY_WORKFLOW')
            or DEFAULT_WORKFLOW).strip()


def load_workflow(path=None):
    path = path or workflow_path()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except OSError:
        raise ComfyError('工作流文件不在：%s' % path)
    except ValueError as e:
        raise ComfyError('工作流不是合法 JSON：%s' % e)
    if not isinstance(data, dict) or not data:
        raise ComfyError('工作流内容为空')
    return data


def _linked(workflow, ref, what):
    if not (isinstance(ref, list) and len(ref) == 2 and str(ref[0]) in workflow):
        raise ComfyError('工作流里找不到%s的连线' % what)
    node = workflow[str(ref[0])]
    if not isinstance(node, dict):
        raise ComfyError('%s节点结构不对' % what)
    return str(ref[0]), node


def resolve(workflow):
    """按连线找采样器、正/反向提示词和空 latent，不写死节点 id。"""
    sampler_id = sampler = None
    for nid, node in workflow.items():
        if isinstance(node, dict) and node.get('class_type') in ('KSampler',
                                                                 'KSamplerAdvanced'):
            sampler_id, sampler = nid, node
            break
    if not sampler:
        raise ComfyError('工作流里没有 KSampler')
    inputs = sampler.get('inputs') or {}
    pos_id, pos = _linked(workflow, inputs.get('positive'), '正向提示词')
    neg_id, neg = _linked(workflow, inputs.get('negative'), '反向提示词')
    lat_id, lat = _linked(workflow, inputs.get('latent_image'), '空 latent')
    if pos.get('class_type') != 'CLIPTextEncode':
        raise ComfyError('正向提示词节点不是 CLIPTextEncode')
    if neg.get('class_type') != 'CLIPTextEncode':
        raise ComfyError('反向提示词节点不是 CLIPTextEncode')
    if lat.get('class_type') not in LATENT_TYPES:
        raise ComfyError('latent 节点不支持：%s' % lat.get('class_type'))
    return {'sampler': sampler_id, 'positive': pos_id, 'negative': neg_id,
            'latent': lat_id}


def _int_in(name, value, lo, hi, multiple=1):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ComfyError('%s 必须是整数' % name)
    if not lo <= n <= hi:
        raise ComfyError('%s要在 %d-%d 之间' % (name, lo, hi))
    if multiple > 1 and n % multiple:
        raise ComfyError('%s要是 %d 的倍数' % (name, multiple))
    return n


def compose(workflow, prompt, negative=None, width=768, height=512,
            steps=20, cfg=7.0, seed=None, batch=1):
    """返回填好参数的副本，不改调用方那份模板。"""
    prompt = (prompt or '').strip()
    if not prompt:
        raise ComfyError('提示词为空')
    width = _int_in('宽度', width, 256, 2048, 8)
    height = _int_in('高度', height, 256, 2048, 8)
    steps = _int_in('步数', steps, 1, 100)
    batch = _int_in('张数', batch, 1, 4)
    try:
        cfg = float(cfg)
    except (TypeError, ValueError):
        raise ComfyError('cfg 必须是数字')
    if not 0 <= cfg <= 30:
        raise ComfyError('cfg 要在 0-30 之间')
    if seed is None:
        seed = int(time.time() * 1000) % 2147483647
    seed = _int_in('seed', seed, 0, 2147483647)

    wf = copy.deepcopy(workflow)
    ids = resolve(wf)
    wf[ids['positive']]['inputs']['text'] = prompt
    if negative is not None:
        wf[ids['negative']]['inputs']['text'] = str(negative)
    latent = wf[ids['latent']]['inputs']
    # 图生图的 latent 来自 VAEEncode，没有尺寸字段；尺寸由参考图决定。
    if 'width' in latent:
        latent['width'] = width
    if 'height' in latent:
        latent['height'] = height
    if 'batch_size' in latent:
        latent['batch_size'] = batch
    sampler = wf[ids['sampler']]
    sampler['inputs']['steps'] = steps
    sampler['inputs']['cfg'] = cfg
    if sampler.get('class_type') == 'KSamplerAdvanced':
        sampler['inputs']['noise_seed'] = seed
    else:
        sampler['inputs']['seed'] = seed
    return wf, seed


def _json(url, timeout, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method='POST' if data else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _first_image(entry):
    for node in (entry.get('outputs') or {}).values():
        if not isinstance(node, dict):
            continue
        for image in node.get('images') or []:
            if isinstance(image, dict) and image.get('filename'):
                return image
    return None


def generate(prompt, width=768, height=512, steps=20, cfg=7.0, seed=None,
             negative=None, workflow=None, timeout=300, url=None):
    """提交一张图并等结果。返回 {png, seed, width, height, elapsed_ms}。"""
    base = (url or base_url()).rstrip('/')
    wf, seed = compose(workflow or load_workflow(), prompt, negative=negative,
                       width=width, height=height, steps=steps, cfg=cfg,
                       seed=seed)
    started = time.monotonic()
    try:
        queued = _json(base + '/prompt', timeout=30,
                       payload={'prompt': wf, 'client_id': uuid.uuid4().hex})
    except urllib.error.HTTPError as e:
        detail = _short(e.read().decode('utf-8', 'replace'), 300)
        if e.code == 400:
            raise ComfyError('ComfyUI 拒绝工作流（400）：%s' % detail)
        raise ComfyError('ComfyUI 返回 HTTP %d：%s' % (e.code, detail))
    except urllib.error.URLError as e:
        raise ComfyError('连不上 ComfyUI（%s）：先启动服务'
                         % _short(e.reason, 80))
    errors = queued.get('node_errors') or {}
    if errors:
        raise ComfyError('工作流节点报错：%s' % _short(json.dumps(
            errors, ensure_ascii=False), 240))
    prompt_id = queued.get('prompt_id')
    if not prompt_id:
        raise ComfyError('ComfyUI 没有返回 prompt_id')

    deadline = started + max(10, timeout)
    entry = None
    while time.monotonic() < deadline:
        try:
            hist = _json(base + '/history/' + urllib.parse.quote(prompt_id),
                         timeout=15)
        except (urllib.error.URLError, ValueError):
            hist = {}
        entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        if entry:
            status = entry.get('status') or {}
            if status.get('status_str') == 'error':
                raise ComfyError('ComfyUI 执行失败：%s' % _short(
                    json.dumps(status.get('messages') or [], ensure_ascii=False),
                    240))
            if _first_image(entry):
                break
        time.sleep(0.5)
    else:
        raise ComfyError('等待出图超时（%ds）' % int(timeout))
    image = _first_image(entry)
    if not image:
        raise ComfyError('ComfyUI 没有返回图片')

    query = urllib.parse.urlencode({
        'filename': image['filename'],
        'subfolder': image.get('subfolder') or '',
        'type': image.get('type') or 'output',
    })
    try:
        with urllib.request.urlopen(base + '/view?' + query,
                                    timeout=60) as r:
            png = r.read()
    except urllib.error.HTTPError as e:
        raise ComfyError('取图失败（HTTP %d）' % e.code)
    except urllib.error.URLError as e:
        raise ComfyError('取图失败：%s' % _short(e.reason, 80))
    if not png.startswith(PNG_MAGIC):
        raise ComfyError('返回内容不是 PNG')
    return {'png': png, 'seed': seed, 'width': width, 'height': height,
            'elapsed_ms': int((time.monotonic() - started) * 1000)}


def health(url=None, timeout=5):
    try:
        data = _json((url or base_url()).rstrip('/') + '/system_stats', timeout)
    except urllib.error.HTTPError as e:
        return {'ok': False, 'error': 'HTTP %d' % e.code}
    except Exception as e:
        return {'ok': False, 'error': _short(e, 80)}
    devices = data.get('devices') or []
    device = devices[0] if devices and isinstance(devices[0], dict) else {}
    free = device.get('vram_free')
    return {'ok': True, 'gpu': device.get('name'),
            'vram_free_mb': int(free / 1024 / 1024) if isinstance(free, (int, float)) else None}


def describe():
    """面板展示用，不触发外部请求。"""
    path = workflow_path()
    return {'url': base_url(),
            'workflow': {'path': path, 'exists': os.path.exists(path)}}
