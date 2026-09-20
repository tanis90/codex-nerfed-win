"""Pure-Python port of ModelTrace's fingerprint core.

Upstream: https://github.com/xqy2006/ModelTrace (MIT, Copyright (c) 2026 xqy2006)
         codex-plugin/modeltrace-guard/scripts/fingerprint-core.mjs

The port is kept numerically identical to the upstream JavaScript so results can be cross-checked
(tests/test_parity.py runs both on the same reference outputs). No numpy required.

Method (from the ModelTrace README): a model is asked for a long "random" integer sequence in 1..355.
Two fingerprints are extracted, a 355-dim Hellinger vector of value frequencies and a 74-dim ordered-block
feature (4 position blocks x 16 value bins + final-digit distribution). Prompt-induced nuisance directions
(estimated from enrollment environments) are projected out, features are compared with per-model centroids
by cosine similarity, and a calibrated softmax turns the fused scores into closed-set attribution
probabilities over the models in the bank.
"""
from __future__ import annotations

import json
import math
import re

VALUE_MIN = 1
VALUE_MAX = 355
DIMENSION = VALUE_MAX - VALUE_MIN + 1
ALPHA = 0.5
ORDERED_BLOCK_WEIGHT = 0.25


def parse_numbers(text) -> list[int]:
    """Longest run of in-range integers; runs are split wherever letters separate two numbers."""
    text = str(text)
    runs: list[list[int]] = []
    current: list[int] = []
    previous_end = 0
    for match in re.finditer(r"\d+", text):
        separator = text[previous_end:match.start()]
        value = int(match.group())
        if current and any(ch.isalpha() for ch in separator):
            runs.append(current)
            current = []
        if VALUE_MIN <= value <= VALUE_MAX:
            current.append(value)
        previous_end = match.end()
    if current:
        runs.append(current)
    return max(runs, key=len) if runs else []


def count_numbers(numbers) -> list[int]:
    counts = [0] * DIMENSION
    for number in numbers:
        counts[number - VALUE_MIN] += 1
    return counts


def _mean(values) -> float:
    return sum(values) / len(values)


def _standardize(values) -> list[float]:
    center = _mean(values)
    variance = _mean([(v - center) ** 2 for v in values])
    scale = max(math.sqrt(variance), 1e-12)
    return [(v - center) / scale for v in values]


def _dot(left, right) -> float:
    return sum(a * b for a, b in zip(left, right))


def _norm(values) -> float:
    return math.sqrt(_dot(values, values))


def _normalized(values) -> list[float]:
    scale = max(_norm(values), 1e-12)
    return [v / scale for v in values]


def _subtract_basis(values, basis) -> list[float]:
    output = list(values)
    for vector in basis or []:
        projection = _dot(output, vector)
        output = [o - projection * b for o, b in zip(output, vector)]
    return output


def hellinger_feature(counts) -> list[float]:
    total = sum(counts) + ALPHA * DIMENSION
    return [math.sqrt((v + ALPHA) / total) for v in counts]


def split_into_four(values) -> list[list[int]]:
    base, remainder = divmod(len(values), 4)
    chunks, start = [], 0
    for index in range(4):
        size = base + (1 if index < remainder else 0)
        chunks.append(values[start:start + size])
        start += size
    return chunks


def ordered_block_feature(numbers) -> list[float]:
    pieces: list[float] = []
    for chunk in split_into_four(numbers):
        bins = [0.5] * 16
        for value in chunk:
            bins[min(15, int(((value - 1) / 355) * 16))] += 1
        total = sum(bins)
        pieces.extend(math.sqrt(v / total) for v in bins)
    last_digits = [0.5] * 10
    for value in numbers:
        last_digits[value % 10] += 1
    last_total = sum(last_digits)
    pieces.extend(math.sqrt(v / last_total) for v in last_digits)
    return pieces


def robust_score_counts(counts, bank) -> list[float]:
    artifact = bank["robust"]["hellinger"]
    feature = hellinger_feature(counts)
    projected = [(v - m) / s for v, m, s in zip(feature, artifact["feature_mean"], artifact["feature_scale"])]
    projected = _normalized(_subtract_basis(projected, artifact["nuisance_basis"]))
    return _standardize([_dot(projected, centroid) for centroid in artifact["centroids"]])


def ordered_block_scores(numbers, bank) -> list[float]:
    artifact = bank["robust"]["ordered_blocks"]
    feature = ordered_block_feature(numbers)
    standardized = [(v - m) / s for v, m, s in zip(feature, artifact["feature_mean"], artifact["feature_scale"])]
    unit = _normalized(standardized)
    environment_scores = [[_dot(unit, centroid) for centroid in centroids] for centroids in artifact["environment_centroids"]]
    template = _standardize([max(scores[i] for scores in environment_scores) for i in range(len(artifact["centroids"]))])
    projected = _normalized(_subtract_basis(standardized, artifact["nuisance_basis"]))
    nuisance = _standardize([_dot(projected, centroid) for centroid in artifact["centroids"]])
    return _standardize([0.5 * t + 0.5 * n for t, n in zip(template, nuisance)])


def robust_score_numbers(numbers, bank) -> list[float]:
    marginal = robust_score_counts(count_numbers(numbers), bank)
    artifact = bank["robust"].get("ordered_blocks")
    weight = float(artifact.get("weight") or 0) if artifact else 0.0
    if not artifact or weight == 0:
        return marginal
    ordered = ordered_block_scores(numbers, bank)
    return [(1 - weight) * m + weight * o for m, o in zip(marginal, ordered)]


def softmax(values) -> list[float]:
    maximum = max(values)
    weights = [math.exp(v - maximum) for v in values]
    total = sum(weights)
    return [w / total for w in weights]


def js_similarity(left, right) -> float:
    left_total = sum(left)
    right_total = sum(right) + ALPHA * DIMENSION
    p = [v / left_total for v in left]
    q = [(v + ALPHA) / right_total for v in right]
    midpoint = [(a + b) / 2 for a, b in zip(p, q)]

    def divergence(values):
        return sum(v * math.log(v / m) for v, m in zip(values, midpoint) if v)

    js = (divergence(p) + divergence(q)) / 2
    return 1 - math.sqrt(js / math.log(2))


def minimum_numbers(expected_count) -> int:
    expected = int(expected_count or 0)
    return max(80, math.ceil(expected * 0.55)) if expected else 80


def analyze_global_outputs(outputs, bank) -> dict:
    """outputs: [{"text": str, "expected_count": int}]. Raises ValueError when no output is usable."""
    model_ids = [m["id"] for m in bank["models"]]
    valid, diagnostics = [], []
    for index, item in enumerate(outputs):
        expected = int(item.get("expected_count") or 0)
        numbers = parse_numbers(item.get("text") or "")
        minimum = minimum_numbers(expected)
        accepted = len(numbers) >= minimum
        diagnostics.append({"index": index, "parsed_numbers": len(numbers), "minimum_numbers": minimum, "accepted": accepted})
        if accepted:
            valid.append({"numbers": numbers, "counts": count_numbers(numbers), "scores": robust_score_numbers(numbers, bank)})
    if not valid:
        raise ValueError("no usable answer: every response was refused or badly truncated")
    combined = [_mean([v["scores"][i] for v in valid]) for i in range(len(model_ids))]
    calibration_key = str(min(len(valid), 3))
    beta = float(bank["calibration"][calibration_key]["beta"])
    probabilities = softmax([beta * s for s in combined])
    pooled = [sum(v["counts"][i] for v in valid) for i in range(DIMENSION)]
    entries = {m["id"]: m for m in bank["models"]}
    family_order = list(dict.fromkeys((m.get("family") or "models") for m in bank["models"]))
    family_names = {}
    for family in family_order:
        family_names[family] = next((m.get("family_name") for m in bank["models"]
                                     if (m.get("family") or "models") == family and m.get("family_name")), family)
    results = []
    for index, model_id in enumerate(model_ids):
        family = entries[model_id].get("family") or "models"
        results.append({"model": model_id, "display_name": entries[model_id].get("display_name") or model_id,
                        "probability": probabilities[index],
                        "profile_similarity": js_similarity(pooled, entries[model_id]["counts"]),
                        "score": combined[index], "family": family, "family_name": family_names[family]})
    results.sort(key=lambda r: r["probability"], reverse=True)
    family_probabilities = {family: sum(r["probability"] for r in results if r["family"] == family) for family in family_order}
    for r in results:
        r["conditional_probability"] = r["probability"] / family_probabilities[r["family"]]
    winning = max(family_order, key=lambda f: family_probabilities[f])
    return {
        "prediction": results[0]["model"], "prediction_name": results[0]["display_name"], "probability": results[0]["probability"],
        "used_outputs": len(valid), "results": results, "diagnostics": diagnostics,
        "calibration": {"queries": calibration_key, "beta": beta, "cv_accuracy": bank["calibration"][calibration_key]["cv_accuracy"]},
        "family_prediction": winning, "family_prediction_name": family_names[winning],
        "family_probability": family_probabilities[winning],
        "family_probabilities": [{"family": f, "display_name": family_names[f], "probability": family_probabilities[f]} for f in family_order],
        "method": bank.get("method", {}).get("name", "Ordered-block + nuisance-Hellinger"),
    }


def load_bank(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def bank_model_ids(bank) -> list[str]:
    return [m["id"] for m in bank["models"]]
