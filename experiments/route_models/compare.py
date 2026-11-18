"""Offline visual-route benchmark. Never sends input to the game.

Candidate image paths are NOT world coordinates or movement authorization.
Uses identical saved inputs/prompt across MLX and OpenAI-compatible local servers.
"""
import argparse
import base64
import hashlib
import importlib.metadata
import json
import math
import sys
import time
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[2]
PROMPT = '''Plan a short WALKING route in this World of Warcraft screenshot.
The controlled player is the upright red-armored humanoid near the center, NOT the
molten quadruped pet, NPCs, text or UI. The goal is the large armored quest NPC
under the leftmost yellow question mark in the nearby central group.
Inspect actual terrain. Identify the player feet, goal feet and solid obstacles.
Draw your own candidate path along connected ground toward that NPC. If direct
approach is obstructed, find a visible way around the obstruction. Choose the
route from the image; there is no prescribed side, turn angle, distance or shape.
Grass is not automatically a wall. Do not walk through solid structures or jump.
Do not invent hidden terrain. An uncertain segment must be explicitly flagged;
if no grounded route is possible, return an empty path and a useful next observation.
All coordinates are [x,y] normalized 0..1000 in THIS image, top left origin.
The avatar's screen center is not automatically its feet. A path is a ground-level
image hypothesis, NOT verified world geometry. Return JSON only, no markdown:
{"player_feet": [x,y] or null, "goal_feet": [x,y] or null,
 "obstacles": [{"kind": "brief name", "polygon": [[x,y], ...]}],
 "path": [[x,y], ...], "uncertain_segments": [zero-based segment index, ...],
 "explanation": "brief image-specific reason for route and any occlusion",
 "next_observation": "what to check before/after the first short movement"}.
Use at most 3 obstacle polygons and 7 path points. Do not repeat points.
'''
CASES = {
    '2137': ('data/screenshots/20261116_213744_camera_after_interact.png', True),
    '1917': ('data/terrain/20261116_191810_825d2ec2/right.png', False),
}


def prepare(out, mirror=False, local=False):
    inputs = out / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    records = []
    for key, (source, crop) in CASES.items():
        path = ROOT / source
        im = Image.open(path).convert('RGB')
        bounds = None
        if crop:
            bounds = tuple(round(v * (im.width if n % 2 == 0 else im.height))
                           for n, v in enumerate((.20, .08, .80, .68)))
            im = im.crop(bounds)
        local_bounds = (220, 300, 700, 540) if local else None
        if local:
            # Same deterministic crop for every model; contains no route labels.
            im = im.crop(local_bounds).resize((960, 480), Image.Resampling.LANCZOS)
        if mirror:
            im = ImageOps.mirror(im)
        name = key + ('-local' if local else '') + ('-mirror' if mirror else '')
        dest = inputs / (name + '.png')
        im.save(dest)
        records.append({'case': name, 'source': source,
                        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'input': str(dest), 'input_sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
                        'size': list(im.size), 'crop_pixels': bounds, 'local_crop_pixels': local_bounds,
                        'mirrored': mirror})
    return records


def parse(raw):
    # Ignore optional reasoning; never splice multiple objects into a valid plan.
    text = raw.split('</think>')[-1].strip()
    if text.startswith('```'):
        text = text.partition('\n')[2].rsplit('```', 1)[0].strip()
    plan = json.loads(text)
    def point(p):
        return (isinstance(p, list) and len(p) == 2 and
                all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1000 for v in p))
    for name in ('player_feet', 'goal_feet'):
        if plan.get(name) is not None and not point(plan[name]):
            raise ValueError(f'invalid {name}')
    route = plan.get('path')
    if not isinstance(route, list) or len(route) > 7 or not all(point(p) for p in route):
        raise ValueError('invalid path')
    if len(route) == 1 or any(a == b for a, b in zip(route, route[1:])):
        raise ValueError('degenerate path')
    obstacles = plan.get('obstacles', [])
    if not isinstance(obstacles, list) or len(obstacles) > 3:
        raise ValueError('invalid obstacles')
    for obstacle in obstacles:
        polygon = obstacle.get('polygon')
        if not isinstance(polygon, list) or len(polygon) < 3 or not all(point(p) for p in polygon):
            raise ValueError('invalid polygon')
    uncertainty = plan.get('uncertain_segments', [])
    if not isinstance(uncertainty, list) or any(type(i) is not int or not 0 <= i < len(route)-1 for i in uncertainty):
        raise ValueError('invalid uncertain segment')
    return plan


def overlay(path, plan, dest):
    im = Image.open(path).convert('RGB')
    d = ImageDraw.Draw(im)
    def xy(p): return (round(p[0] * im.width / 1000), round(p[1] * im.height / 1000))
    for obs in plan.get('obstacles', []):
        points = [xy(p) for p in obs['polygon']]
        d.line(points + points[:1], fill='red', width=3)
    route = plan.get('path', [])
    for i, (a, b) in enumerate(zip(route, route[1:])):
        d.line([xy(a), xy(b)], fill='orange' if i in plan.get('uncertain_segments', []) else 'cyan', width=4)
    for i, p in enumerate(route):
        x, y = xy(p)
        d.ellipse((x-4, y-4, x+4, y+4), fill='cyan')
        d.text((x+6, y+3), str(i), fill='white', stroke_width=1, stroke_fill='black')
    for name, color, label in [('player_feet', 'magenta', 'P'), ('goal_feet', 'yellow', 'G')]:
        if plan.get(name) is not None:
            x, y = xy(plan[name]); d.ellipse((x-6,y-6,x+6,y+6), outline=color,width=3)
            d.text((x+7,y-14), label, fill=color, stroke_width=1, stroke_fill='black')
    im.save(dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backend', choices=['mlx', 'http', 'ollama'], required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--endpoint', default='http://127.0.0.1:8011/v1/chat/completions')
    ap.add_argument('--out', type=Path, default=ROOT/'data/terrain/model-route-comparison-20261116')
    ap.add_argument('--label', required=True)
    ap.add_argument('--mirror', action='store_true')
    ap.add_argument('--local', action='store_true', help='Controlled same-frame near-field crop ablation')
    ap.add_argument('--thinking', action='store_true', help='Enable supported model reasoning template')
    ap.add_argument('--tokens', type=int, default=1200)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    records = prepare(args.out, args.mirror, args.local)
    instruction = PROMPT.replace('leftmost yellow', 'rightmost yellow') if args.mirror else PROMPT
    (args.out/(args.label+'-prompt.txt')).write_text(instruction)
    runner = None
    load_s = None
    runtime = {}
    for package in ('mlx', 'mlx-vlm', 'transformers', 'pillow'):
        try:
            runtime[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    if args.backend == 'mlx':
        sys.path.insert(0, str(ROOT))
        from experiments.qwen3_vl.run import VisionModel
        runner = VisionModel(Path(args.model))
        load_s = runner.load_s
    results = []
    for record in records:
        result = dict(record, model=args.model, backend=args.backend, prompt=instruction,
                      temperature=0, max_tokens=args.tokens, thinking=args.thinking,
                      load_s=load_s, client_runtime=runtime, world_route_verified=False)
        started = time.perf_counter()
        try:
            if runner is not None:
                im = Image.open(record['input']).convert('RGB')
                prompt = runner.template(runner.processor, runner.config, instruction, num_images=1,
                                         enable_thinking=args.thinking)
                output = runner.generate(runner.model, runner.processor, prompt, [im],
                                         max_tokens=args.tokens, temperature=0, verbose=False)
                raw = output.text
                result['usage'] = {'prompt_tokens': output.prompt_tokens, 'completion_tokens': output.generation_tokens}
            else:
                encoded = base64.b64encode(Path(record['input']).read_bytes()).decode()
                payload = {'model':args.model, 'temperature':0, 'max_tokens':args.tokens,
                           'chat_template_kwargs':{'enable_thinking':args.thinking},
                           'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+encoded}},
                                                                {'type':'text','text':instruction}]}]}
                if args.backend == 'ollama':
                    payload = {'model': args.model, 'stream': False, 'keep_alive': 0,
                               'options': {'temperature': 0, 'num_predict': args.tokens},
                               'messages': [{'role': 'user', 'content': instruction, 'images': [encoded]}]}
                req = urllib.request.Request(args.endpoint, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req, timeout=180) as response:
                    body = json.load(response)
                if args.backend == 'ollama':
                    raw = body['message']['content']
                    result['usage'] = {k: body.get(k) for k in ('prompt_eval_count', 'eval_count', 'load_duration', 'total_duration')}
                    result['finish_reason'] = body.get('done_reason')
                else:
                    raw = body['choices'][0]['message']['content'] or ''
                    result['usage'] = body.get('usage')
                    result['finish_reason'] = body['choices'][0].get('finish_reason')
                    result['reasoning'] = body['choices'][0]['message'].get('reasoning_content')
            result['raw_output'] = raw
            result['elapsed_s'] = round(time.perf_counter()-started, 3)
            plan = parse(raw)
            result['plan'] = plan
            result['syntax_valid'] = True
            result['requires_visual_review'] = True
            rendered = args.out / (args.label+'-'+record['case']+'.png')
            overlay(record['input'], plan, rendered)
            result['overlay'] = str(rendered)
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            result.setdefault('elapsed_s', round(time.perf_counter()-started, 3))
        results.append(result)
        (args.out/(args.label+'.json')).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps({'case':record['case'], 'model':args.model, 'elapsed_s':result['elapsed_s'],
                          'plan':result.get('plan'), 'error':result.get('error')},ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
