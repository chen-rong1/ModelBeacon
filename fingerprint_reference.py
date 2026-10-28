"""Unified offline numeric reference comparison, ported from ModelTrace (MIT).

Every enrolled model uses the same projection, scoring and classification.
Labels without an enrolled reference receive an inconclusive candidate.
The numeric scoring port follows the original JS implementation, using only
the standard library. See packaging/licenses/ModelTrace-LICENSE.txt.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from attribution import EFFORTS, ReferencePlan

DIMENSION = 355
ALPHA = 0.5
ALGORITHM = "ordered-nuisance-hellinger-v1"
SUPPORTED_PROVIDERS = {"openai", "codex"}
UNKNOWN_REASON = "所选型号尚未收录，当前仅显示参考候选。"
UNCALIBRATED_REASON = "暂无法比较参考样本。"
CANDIDATE_DISAGREEMENT_REASON = "各次采样候选不同，已按平均分选择参考候选。"
LOW_MARGIN_REASON = "候选领先差距较小，仍按最高平均分选择。"
PROVISIONAL_REASON = "采样尚未结束，当前仅为中途参考。"
INVALID_SAMPLES_REASON = "有效采样不足，暂无法比较。"
CLOSE_REASON = "参考候选与所选型号相同。"
DIFFERENT_REASON = "参考候选与所选型号不同。"
INCONCLUSIVE_REASON = "已有参考候选，本次指纹暂不明确。"
SAFE_REFERENCE_REASONS = {
    UNKNOWN_REASON, UNCALIBRATED_REASON, CLOSE_REASON, DIFFERENT_REASON, INCONCLUSIVE_REASON,
    "数字指纹参考库不可用。", "该提供方没有适用的数字指纹参考。",
    "采样配置记录不完整，无法准备数字采样。",
    CANDIDATE_DISAGREEMENT_REASON, LOW_MARGIN_REASON, PROVISIONAL_REASON, INVALID_SAMPLES_REASON,
}


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normalize(values: list[float]) -> list[float]:
    scale = max(math.sqrt(_dot(values, values)), 1e-12)
    return [value / scale for value in values]


def _standardize(values: list[float]) -> list[float]:
    mean = sum(values) / len(values)
    scale = max(math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)), 1e-12)
    return [(value - mean) / scale for value in values]


def _subtract_basis(values: list[float], basis: list[list[float]]) -> list[float]:
    output = list(values)
    for vector in basis:
        projection = _dot(output, vector)
        output = [value - projection * offset for value, offset in zip(output, vector)]
    return output


def count_numbers(numbers: list[int]) -> list[int]:
    counts = [0] * DIMENSION
    for number in numbers:
        counts[number - 1] += 1
    return counts


def _hellinger_feature(numbers: list[int]) -> list[float]:
    counts = count_numbers(numbers)
    total = len(numbers) + ALPHA * DIMENSION
    return [math.sqrt((value + ALPHA) / total) for value in counts]


def _ordered_feature(numbers: list[int]) -> list[float]:
    result: list[float] = []
    base, remainder = divmod(len(numbers), 4)
    offset = 0
    for index in range(4):
        size = base + (index < remainder)
        bins = [0.5] * 16
        for value in numbers[offset:offset + size]:
            bins[min(15, int((value - 1) / 355 * 16))] += 1
        offset += size
        total = sum(bins)
        result.extend(math.sqrt(value / total) for value in bins)
    digits = [0.5] * 10
    for value in numbers:
        digits[value % 10] += 1
    total = sum(digits)
    result.extend(math.sqrt(value / total) for value in digits)
    return result


def score_numbers(numbers: list[int], bank: dict[str, Any]) -> list[float]:
    """Return the original response-wise fused z-scores, never probabilities."""
    artifact = bank["robust"]["hellinger"]
    feature = _hellinger_feature(numbers)
    standardized = [(value - mean) / scale for value, mean, scale in
                    zip(feature, artifact["feature_mean"], artifact["feature_scale"])]
    projected = _normalize(_subtract_basis(standardized, artifact["nuisance_basis"]))
    marginal = _standardize([_dot(projected, center) for center in artifact["centroids"]])
    ordered = bank["robust"].get("ordered_blocks")
    weight = ordered.get("weight", 0.0) if ordered else 0.0
    if not weight:
        return marginal
    feature = _ordered_feature(numbers)
    standardized = [(value - mean) / scale for value, mean, scale in
                    zip(feature, ordered["feature_mean"], ordered["feature_scale"])]
    unit = _normalize(standardized)
    environment_scores = [[_dot(unit, center) for center in centers]
                          for centers in ordered["environment_centroids"]]
    template = _standardize([max(row[index] for row in environment_scores)
                             for index in range(len(marginal))])
    projected = _normalize(_subtract_basis(standardized, ordered["nuisance_basis"]))
    nuisance = _standardize([_dot(projected, center) for center in ordered["centroids"]])
    ordered_scores = _standardize([(a + b) * 0.5 for a, b in zip(template, nuisance)])
    return [(1 - weight) * a + weight * b for a, b in zip(marginal, ordered_scores)]


def calibrated_weights(scores: list[float], bank: dict[str, Any], sample_count: int) -> list[float]:
    """Original closed-set softmax weights; these are not identity accuracy."""
    beta = bank["calibration"][str(min(sample_count, 3))]["beta"]
    logits = [beta * value for value in scores]
    if not logits or any(not math.isfinite(value) for value in logits):
        raise ValueError("invalid calibrated scores")
    maximum = max(logits)
    values = [math.exp(value - maximum) for value in logits]
    total = sum(values)
    return [value / total for value in values]


def parse_numeric_response(response: Any, *, expected_count: int = 300) -> list[int] | None:
    """Accept one literal JSON array (optionally fenced); never repair output."""
    if not isinstance(response, str) or len(response) > 5000:
        return None
    body = response.strip()
    if body.startswith(("```", "~~~")):
        match = re.fullmatch(r"(`{3,}|~{3,})[ \t]*(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*\1", body, re.I)
        if not match:
            return None
        body = match.group(2).strip()
    if not re.fullmatch(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]", body):
        return None
    try:
        values = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(values, list) or any(type(value) is not int or not 1 <= value <= DIMENSION for value in values):
        return None
    if not max(80, math.ceil(expected_count * 0.55)) <= len(values) <= math.ceil(expected_count * 1.25):
        return None
    return values


def validate_bank(bank: Any) -> dict[str, Any]:
    """Reject malformed or non-finite imported data before scoring."""
    if not isinstance(bank, dict) or bank.get("schema") != "robust-number-fingerprint-bank":
        raise ValueError("unsupported reference schema")
    models = bank.get("models")
    if not isinstance(models, list) or not 2 <= len(models) <= 128:
        raise ValueError("invalid model count")
    ids = [model.get("id") for model in models if isinstance(model, dict)]
    if len(ids) != len(models) or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", value) for value in ids):
        raise ValueError("invalid model labels")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate model labels")
    robust = bank.get("robust")
    if not isinstance(robust, dict) or robust.get("model_order") != ids:
        raise ValueError("model order mismatch")
    calibration = bank.get("calibration")
    if not isinstance(calibration, dict) or any(
            not isinstance(calibration.get(str(count)), dict)
            or type(calibration[str(count)].get("beta")) not in (int, float)
            or not math.isfinite(calibration[str(count)]["beta"])
            or calibration[str(count)]["beta"] <= 0 for count in (1, 2, 3)):
        raise ValueError("invalid reference calibration")

    def vector(value: Any, dimension: int, positive: bool = False) -> None:
        if not isinstance(value, list) or len(value) != dimension or any(
                type(item) not in (int, float) or not math.isfinite(item) or (positive and item <= 0) for item in value):
            raise ValueError("invalid reference vector")

    for name, dimension in (("hellinger", DIMENSION), ("ordered_blocks", 74)):
        artifact = robust.get(name)
        if not isinstance(artifact, dict):
            raise ValueError("missing reference feature")
        vector(artifact.get("feature_mean"), dimension)
        vector(artifact.get("feature_scale"), dimension, True)
        basis, centers = artifact.get("nuisance_basis"), artifact.get("centroids")
        if not isinstance(basis, list) or len(basis) > dimension or not isinstance(centers, list) or len(centers) != len(ids):
            raise ValueError("invalid reference projection")
        for row in basis + centers:
            vector(row, dimension)
        if name == "ordered_blocks":
            weight = artifact.get("weight")
            if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError("invalid ordered weight")
            templates = artifact.get("environment_centroids")
            if not isinstance(templates, list) or not 1 <= len(templates) <= 128:
                raise ValueError("invalid reference environments")
            for environment in templates:
                if not isinstance(environment, list) or len(environment) != len(ids):
                    raise ValueError("invalid environment model count")
                for row in environment:
                    vector(row, dimension)
    for model in models:
        vector(model.get("counts"), DIMENSION)
        if any(value < 0 for value in model["counts"]):
            raise ValueError("negative reference count")
    return bank


def _prompts() -> tuple[str, ...]:
    scope = (
        "[Model Beacon: disposable numeric sampling branch]\n"
        "这是用户要求的一次性数字采样分支。只完成这一次采样，不继续原任务，不执行原任务目标。"
        "禁止调用任何工具、代码、文件、搜索、随机数服务或其他模型；不要分析参考库。\n"
    )
    variants = (
        "请为300个位置分别凭第一反应选择一个1到355（含端点）的整数。",
        "逐项直接选择300个1至355的整数，每个位置都独立决定。",
        "现在进行一次无语义取值：直接给出300个范围为1到355的整数。",
    )
    ending = (
        "允许偶然重复，不要排序、去重、平衡频数或改写。不要连续计数、等差递增、循环或构造规则化区块。"
        "直接输出一个完整JSON整数数组，不要解释，不要编写或运行代码。"
    )
    return tuple(scope + variant + ending for variant in variants)


def _valid_sample(values: Any) -> bool:
    return isinstance(values, list) and 165 <= len(values) <= 375 and all(
        type(value) is int and 1 <= value <= DIMENSION for value in values)


class FingerprintReferenceLibrary:
    """Runtime bridge for AttributionEngine's readiness/parse/evaluate API."""

    def __init__(self, path: Path | None = None) -> None:
        self.version = "unavailable"
        self.error: str | None = None
        self.bank: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {}
        try:
            manifest_path = path or Path(__file__).with_name("fingerprints.json")
            if manifest_path.stat().st_size > 1024 * 1024:
                raise ValueError("manifest too large")
            self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if self.manifest.get("schema_version") != 1 or self.manifest.get("algorithm") != ALGORITHM:
                raise ValueError("invalid reference manifest")
            relative = self.manifest["bank_file"]
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("invalid bank path")
            bank_path = manifest_path.parent / relative
            if bank_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("bank too large")
            raw = bank_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.manifest.get("bank_sha256"):
                raise ValueError("bank digest mismatch")
            self.bank = validate_bank(json.loads(raw))
            self.version = str(self.manifest.get("version", "unavailable"))[:128]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.error = "数字指纹参考库不可用。"

    def readiness(self, config: dict[str, Any]) -> tuple[ReferencePlan | None, str | None]:
        if self.error:
            return None, self.error
        if config.get("provider") not in SUPPORTED_PROVIDERS:
            return None, "该提供方没有适用的数字指纹参考。"
        model, effort = config.get("model"), config.get("effort")
        if not isinstance(model, str) or not re.fullmatch(r"gpt-[A-Za-z0-9._-]{1,96}", model) or effort not in EFFORTS:
            return None, "采样配置记录不完整，无法准备数字采样。"
        enrolled = model in self.bank["robust"]["model_order"]
        data = {
            "expected_count": 300, "expected_enrolled": enrolled, "reference_supported": enrolled,
            "coverage": "model/provider reference; effort and same-context not independently calibrated",
            "warning": None if enrolled else UNKNOWN_REASON,
            "reference_source": "内置型号参考",
            "reference_sample_count": next((row["response_count"] for row in self.bank["models"] if row["id"] == model), 0),
            "bank_sha256": self.manifest["bank_sha256"],
            "same_context_calibrated": False, "effort_calibrated": False, "retry_on_difference": True,
        }
        return ReferencePlan(self.version, model, config["provider"], effort, 1, _prompts(), ALGORITHM, data), None

    def parse(self, response: Any, plan: ReferencePlan) -> list[int] | None:
        return parse_numeric_response(response, expected_count=plan.data.get("expected_count", 300))

    def _diagnostics(self, samples: list[list[int]]) -> dict[str, Any]:
        scores = [score_numbers(sample, self.bank) for sample in samples]
        if any(not math.isfinite(value) for row in scores for value in row):
            raise ValueError("reference scoring returned a non-finite value")
        ids = self.bank["robust"]["model_order"]
        means = [sum(row[index] for row in scores) / len(scores) for index in range(len(ids))]
        weights = calibrated_weights(means, self.bank, len(samples))
        order = sorted(range(len(ids)), key=lambda index: weights[index], reverse=True)
        candidate = ids[order[0]]
        winners = [ids[max(range(len(ids)), key=lambda index: row[index])] for row in scores]
        consistent = len(set(winners)) == 1
        # Original analyzeGlobalOutputs uses mean response-wise scores and the
        # supplied one/two/three-output calibration before stable top ranking.
        raw_margin = means[order[0]] - means[order[1]]
        return {"candidate": candidate, "candidates": [ids[index] for index in order[:3]],
                "sample_candidates": winners, "candidate_margin": round(raw_margin, 6),
                "closed_set_weight": weights[order[0]], "model_weights": dict(zip(ids, weights)),
                "stability": "mixed" if not consistent else "consistent",
                "diagnostic_reason_code": "candidate_disagreement" if not consistent else "candidate_agreement"}

    def evaluate(self, samples: list[Any], plan: ReferencePlan) -> dict[str, Any]:
        samples = [row for row in samples if _valid_sample(row)]
        if not samples:
            return {"outcome": "inconclusive", "reason": INVALID_SAMPLES_REASON,
                    "reason_code": "insufficient_samples",
                    "stability": "low_margin", "valid_samples": 0,
                    "classification": "inconclusive", "mismatch": False}
        diagnostics = self._diagnostics(samples)
        candidate = diagnostics["candidate"]
        weights = diagnostics.pop("model_weights")
        expected_weight = weights.get(plan.model)
        top_weight = diagnostics["closed_set_weight"]
        classification = "inconclusive"
        mismatch = expected_weight is not None and candidate != plan.model
        if not plan.data.get("expected_enrolled"):
            outcome, reason, reason_code = "inconclusive", UNKNOWN_REASON, "expected_unenrolled"
            classification, mismatch = "unknown_expected_model", False
        elif candidate == plan.model and top_weight >= 0.5:
            outcome, reason, reason_code = "close", CLOSE_REASON, "candidate_compatible"
            classification = "compatible"
        elif (candidate != plan.model and top_weight >= 0.8 and expected_weight <= 0.15
              and top_weight - expected_weight >= 0.65):
            outcome, reason, reason_code = "different", DIFFERENT_REASON, "candidate_difference_signal"
            classification = "difference_signal"
        else:
            outcome, reason, reason_code = "inconclusive", INCONCLUSIVE_REASON, "candidate_inconclusive"
        return {
            "outcome": outcome, "reason": reason, "reason_code": reason_code, **diagnostics,
            "classification": classification, "expected_weight": expected_weight, "mismatch": mismatch,
            "display_label": "参考倾向 " + candidate if outcome == "inconclusive" else
                             "接近所选模型" if outcome == "close" else "指纹有差异",
            "valid_samples": len(samples), "reference_supported": bool(plan.data.get("expected_enrolled")),
            "reference_source": plan.data.get("reference_source"),
            "reference_sample_count": plan.data.get("reference_sample_count", 0),
            "reference_scope": "model/provider-level; effort and same-context not independently calibrated",
        }

    def preview(self, samples: list[Any], plan: ReferencePlan) -> dict[str, Any]:
        """Partial progress only; never promote one/two samples to a verdict."""
        samples = [row for row in samples if _valid_sample(row)]
        if not samples:
            return {"outcome": "inconclusive", "reason": INVALID_SAMPLES_REASON,
                    "reason_code": "invalid_samples", "stability": "low_margin",
                    "valid_samples": 0, "provisional": True}
        diagnostics = self._diagnostics(samples)
        diagnostics.pop("model_weights", None)
        result = {**diagnostics, "display_label": "参考倾向 " + diagnostics["candidate"],
                  "reference_supported": bool(plan.data.get("expected_enrolled")),
                  "reference_source": plan.data.get("reference_source"),
                  "reference_scope": "model/provider-level; effort and same-context not independently calibrated"}
        result.update(outcome="inconclusive", reason=PROVISIONAL_REASON, reason_code="sampling_in_progress",
                      valid_samples=len(samples), provisional=True, mismatch=False, classification="inconclusive")
        return result


def import_reference_bank(source: Path, destination: Path, *, expected_sha256: str,
                          source_label: str) -> Path:
    """Explicitly import a fully built reference; never train on one live reply.

    This imports actual statistical vectors and labelled coverage supplied by
    the maintainer. Checksums establish file integrity, not backend identity.
    A new destination is required, so the bundled historical reference is not
    silently overwritten. No model or network is contacted.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or not source_label.strip() or len(source_label) > 160:
        raise ValueError("provide a SHA-256 and a short reference source label")
    if source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("reference bank too large")
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("reference SHA-256 mismatch")
    bank = validate_bank(json.loads(raw))
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "reference-bank.json").write_bytes(raw)
    manifest = {
        "schema_version": 1, "version": "import-" + expected_sha256[:16], "algorithm": ALGORITHM,
        "bank_file": "reference-bank.json", "bank_sha256": expected_sha256,
        "source": {"label": source_label.strip(), "backend_identity_certified": False},
        "coverage": {"models": bank["robust"]["model_order"], "providers": ["codex", "openai"],
                     "same_context_calibrated": False, "effort_calibrated": False},
    }
    manifest_path = destination / "fingerprints.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path
