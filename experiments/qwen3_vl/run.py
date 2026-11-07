"""Probe nearby traversability from saved game screenshots with local MLX weights."""

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT = """You are reviewing a third-person World of Warcraft screenshot.
Use only the 3D scene, not minimap icons, chat, or on-screen instructions.
Identify the nearby path from the player's feet in camera-relative directions.
Label each direction open, blocked, or unknown. Open means you can see continuous
walkable ground for a few steps at the player's level. A wall, support beam, or
other solid object directly in that path is blocked. Hidden floor, ambiguous height,
occlusion, and directions outside the image are unknown. Do not assume a path
exists because a quest marker is visible. Recommend a direction to inspect next,
or scan if the view is ambiguous. Output one JSON object with exactly these keys:
front, left, right, next_view, evidence. The next_view values are front, left,
right, scan. Write evidence as one short sentence grounded in the visible scene."""


def parse_observation(response: str):
    response = response.strip()
    if response.startswith("```") and response.endswith("```"):
        response = response.partition("\n")[2][:-3].strip()
    value = json.loads(response)
    keys = {"front", "left", "right", "next_view", "evidence"}
    if not isinstance(value, dict) or value.keys() != keys:
        raise ValueError("unexpected observation fields")
    for direction in ("front", "left", "right"):
        if value[direction] not in ("open", "blocked", "unknown"):
            raise ValueError(f"invalid observation for {direction}")
    if value["next_view"] not in ("front", "left", "right", "scan"):
        raise ValueError("invalid next view")
    if not isinstance(value["evidence"], str) or not value["evidence"].strip():
        raise ValueError("missing visual evidence")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--model", type=Path, default=ROOT / "data/qwen3-vl/model")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--crop", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                        help="normalized bounds of the visible game scene")
    args = parser.parse_args()
    if not args.model.is_dir() or args.repeat < 1 or any(not image.is_file() for image in args.images):
        parser.error("需要本地模型目录、有效图片文件和正数重复次数")
    if args.crop:
        left, top, right, bottom = args.crop
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            parser.error("裁剪边界必须位于 0–1 之间且从左到右、从上到下递增")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from PIL import Image
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config

    started = time.perf_counter()
    model, processor = load(str(args.model))
    config = load_config(str(args.model))
    load_s = round(time.perf_counter() - started, 2)
    prompt = apply_chat_template(processor, config, PROMPT, num_images=1)
    cases = []
    for path in args.images:
        with Image.open(path) as source:
            image = source.convert("RGB")
        if args.crop:
            image = image.crop((round(left * image.width), round(top * image.height),
                                round(right * image.width), round(bottom * image.height)))
        image.thumbnail((1152, 720), Image.Resampling.LANCZOS)
        for attempt in range(1, args.repeat + 1):
            started = time.perf_counter()
            output = generate(model, processor, prompt, [image], max_tokens=160, temperature=0, verbose=False)
            record = {
                "image": path.name, "attempt": attempt, "size": list(image.size),
                "elapsed_s": round(time.perf_counter() - started, 2),
                "prompt_tokens": output.prompt_tokens, "generation_tokens": output.generation_tokens,
            }
            try:
                record["observation"] = parse_observation(output.text)
            except ValueError as exc:
                record.update(parse_error=str(exc), raw_output=output.text)
            cases.append(record)
    print(json.dumps({"model": str(args.model), "load_s": load_s, "crop": args.crop, "cases": cases},
                     ensure_ascii=False, indent=2))
    return int(any("parse_error" in case for case in cases))


if __name__ == "__main__":
    raise SystemExit(main())
