"""Measure local StartLux decisions on synthetic state. Uses no game runtime."""

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from cases import PAIRS

MIN_CONFIDENCE = 0.55
MAX_NONE_PROBABILITY = 0.20
MAX_STATE_AGE_MS = 2500


def recommend(opener, port, timeout, state, instructions, criteria):
    if state["observation_age_ms"] > MAX_STATE_AGE_MS:
        return {"recommendation": None, "reason": "stale_state"}

    request = Request(
        f"http://127.0.0.1:{port}/v1/systemone",
        data=json.dumps({
            "state": state,
            "questions": {"action": {
                "type": "choice", "instructions": instructions, "criteria": criteria,
            }},
            "none_check": True,
            "allow_none": False,
        }, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    answer = result["answers"]["action"]
    choice = answer["choice"]
    confidence = answer["confidence"]
    none_probability = answer["none_probability"]
    probabilities = answer["probabilities"]
    if choice not in criteria or probabilities.keys() != criteria.keys():
        raise ValueError(f"服务返回的候选与请求不符：{answer}")
    if any(not 0 <= value <= 1 for value in (confidence, none_probability, *probabilities.values())):
        raise ValueError(f"服务返回了无效概率：{answer}")

    reason = None
    if none_probability >= MAX_NONE_PROBABILITY:
        reason = "no_option_fits"
    elif confidence < MIN_CONFIDENCE:
        reason = "low_confidence"
    return {
        "model": result["model"],
        "choice": choice,
        "confidence": confidence,
        "none_probability": none_probability,
        "probabilities": probabilities,
        "recommendation": choice if reason is None else None,
        "reason": reason,
        "latency_ms": elapsed_ms,
    }


def evaluate(opener, port, timeout):
    results = []
    for order in ("declared", "reversed"):
        for pair, domain, instructions, criteria, before, after in PAIRS:
            baseline, other = criteria
            ordered = criteria if order == "declared" else dict(reversed(list(criteria.items())))
            for variant, facts, expected in (("a", before, baseline), ("b", after, other)):
                state = {"scenario": domain, "observation_age_ms": 100, **facts}
                result = recommend(opener, port, timeout, state, instructions, ordered)
                results.append({
                    "pair": pair, "variant": variant, "order": order, "domain": domain,
                    "state": state, "expected": expected, "baseline": baseline, **result,
                })
                print(f"{pair}/{variant:<1} {order:8} "
                      f"{'OK' if result['choice'] == expected else 'MISS':4} "
                      f"{result['choice']:16} conf={result['confidence']:.3f} "
                      f"{result['latency_ms']:.1f} ms"
                      + (f" ({result['reason']})" if result["reason"] else ""), flush=True)
    return results


def summarize(results):
    latency = sorted(row["latency_ms"] for row in results[1:])
    declared = [row for row in results if row["order"] == "declared"]
    reversed_order = [row for row in results if row["order"] == "reversed"]
    accepted = [row for row in results if row["recommendation"] is not None]
    return {
        "model": results[0]["model"],
        "samples": len(results),
        "static_priority_correct": sum(row["baseline"] == row["expected"] for row in results),
        "model_top1_correct": sum(row["choice"] == row["expected"] for row in results),
        "model_pair_correct_both_orders": sum(
            all(row["choice"] == row["expected"] for row in results if row["pair"] == pair[0])
            for pair in PAIRS
        ),
        "pairs": len(PAIRS),
        "stable_under_reorder": sum(a["choice"] == b["choice"] for a, b in zip(declared, reversed_order)),
        "unique_states": len(declared),
        "accepted": len(accepted),
        "accepted_correct": sum(row["recommendation"] == row["expected"] for row in accepted),
        "first_request_ms": results[0]["latency_ms"],
        "subsequent_p50_ms": round(statistics.median(latency), 1),
        "subsequent_p95_ms": latency[math.ceil(len(latency) * 0.95) - 1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, default=Path("data/startlux/results.json"))
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))

    # An absent opener makes this fail immediately if stale state reaches HTTP.
    guard = recommend(None, args.port, args.timeout, {"observation_age_ms": 3000}, "", {})
    try:
        results = evaluate(opener, args.port, args.timeout)
    except HTTPError as exc:
        parser.exit(1, f"StartLux HTTP {exc.code}: {exc.read().decode(errors='replace')}\n")
    except (URLError, TimeoutError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"StartLux 实验失败：{exc}\n")

    summary = summarize(results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "summary": summary,
        "thresholds": {
            "min_confidence": MIN_CONFIDENCE,
            "max_none_probability": MAX_NONE_PROBABILITY,
            "max_state_age_ms": MAX_STATE_AGE_MS,
        },
        "stale_state_guard": guard,
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果已写入 {args.output}")


if __name__ == "__main__":
    main()
