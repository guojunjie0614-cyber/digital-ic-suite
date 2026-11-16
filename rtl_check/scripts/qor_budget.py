"""Evaluate structural QoR budgets and non-blocking trend deltas."""

from __future__ import annotations

import fnmatch
from pathlib import Path

BUDGET_METRICS = {
    "latch_bits_max": ("latch_bits", "max"),
    "comb_loops_max": ("comb_loops", "max"),
    "stuck_output_bits_max": ("stuck_output_bits", "max"),
    "undriven_output_bits_max": ("undriven_output_bits", "max"),
    "max_fanout_max": ("max_fanout", "max"),
    "ff_bits_max": ("ff_bits", "max"),
    "memory_bits_min": ("memory_bits", "min"),
    "memory_bits_max": ("memory_bits", "max"),
    "memory_objects_min": ("memory_objects", "min"),
    "memory_objects_max": ("memory_objects", "max"),
    "mux_data_bits_max": ("mux_data_bits", "max"),
    "gate_depth_max": ("gate_depth", "max"),
    "word_depth_max": ("word_depth", "max"),
    "logic_depth_max": ("word_depth", "max"),
}

METRIC_ALIASES = {
    "ff_bits_estimate": "ff_bits",
    "mux_data_bits_estimate": "mux_data_bits",
    "logic_depth": "word_depth",
}

RESOURCE_OPERATIONS = {"multiply", "add_sub", "compare", "shift", "divide_mod", "mux"}
RESOURCE_FIELDS = {
    "operation", "match", "expected", "min", "max", "a_width", "b_width",
    "y_width", "reuse_factor", "allow_overlap",
}
STORAGE_FIELDS = {
    "implementation", "match", "width", "depth", "bits", "banks",
    "read_ports", "write_ports", "read_latency", "allow_overlap",
}
COUNT_FIELDS = {"expected", "min", "max", "a_width", "b_width", "y_width", "reuse_factor"}
STORAGE_INT_FIELDS = {"width", "depth", "bits", "banks", "read_ports", "write_ports", "read_latency"}
MATCH_FIELDS = {"id", "path", "name", "module", "instance", "cell_type"}
RESOURCE_MATCH_FIELDS = MATCH_FIELDS | {"operation", "a_width", "b_width", "y_width"}
STORAGE_MATCH_FIELDS = MATCH_FIELDS | {
    "width", "depth", "bits", "read_ports", "write_ports", "read_latency",
}


def load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "PyYAML is required when using --budget; install with: pip install pyyaml"
        ) from exc
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"Expected a YAML mapping in {path}")
    return data


def _selector_error(selector, fields: set[str]) -> str | None:
    if selector is None:
        return None
    if not isinstance(selector, dict):
        return "match must be a mapping"
    unknown = set(selector) - fields
    if unknown:
        return f"unknown match fields: {sorted(unknown, key=str)}"
    return None


def _shape_errors(budget: dict) -> list[dict]:
    errors: list[dict] = []
    resources = budget.get("resources") or {}
    storage = budget.get("storage") or {}
    limits = budget.get("limits") if "limits" in budget else (budget.get("audit") or {})

    if not isinstance(resources, dict):
        errors.append({"name": "budget:resources", "status": "fail", "reason": "resources must be a mapping"})
        resources = {}
    if not isinstance(storage, dict):
        errors.append({"name": "budget:storage", "status": "fail", "reason": "storage must be a mapping"})
        storage = {}
    if not isinstance(limits, dict):
        errors.append({"name": "budget:limits", "status": "fail", "reason": "limits must be a mapping"})
        limits = {}

    for name, rule in resources.items():
        tag = f"resource:{name}"
        if not isinstance(rule, dict):
            errors.append({"name": tag, "status": "fail", "reason": "resource rule must be a mapping"})
            continue
        unknown = set(rule) - RESOURCE_FIELDS
        if unknown:
            errors.append({"name": tag, "status": "fail", "reason": f"unknown fields: {sorted(unknown)}"})
        if rule.get("operation") not in RESOURCE_OPERATIONS:
            errors.append({"name": tag, "status": "fail", "reason": "unsupported or missing operation"})
        if not any(rule.get(f) is not None for f in ("expected", "min", "max")):
            errors.append({"name": tag, "status": "fail", "reason": "expected/min/max required"})
        if reason := _selector_error(rule.get("match"), RESOURCE_MATCH_FIELDS):
            errors.append({"name": tag, "status": "fail", "reason": reason})
        for f in COUNT_FIELDS:
            v = rule.get(f)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                errors.append({"name": tag, "status": "fail", "reason": f"{f} must be a non-negative integer"})

    for name, rule in storage.items():
        tag = f"storage:{name}"
        if not isinstance(rule, dict):
            errors.append({"name": tag, "status": "fail", "reason": "storage rule must be a mapping"})
            continue
        unknown = set(rule) - STORAGE_FIELDS
        if unknown:
            errors.append({"name": tag, "status": "fail", "reason": f"unknown fields: {sorted(unknown)}"})
        if reason := _selector_error(rule.get("match"), STORAGE_MATCH_FIELDS):
            errors.append({"name": tag, "status": "fail", "reason": reason})
        for f in STORAGE_INT_FIELDS:
            v = rule.get(f)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                errors.append({"name": tag, "status": "fail", "reason": f"{f} must be a non-negative integer"})

    unknown_limits = set(limits) - (set(BUDGET_METRICS) | {"regression_pct_max"})
    if unknown_limits:
        errors.append({"name": "budget:limits", "status": "fail", "reason": f"unknown limit fields: {sorted(unknown_limits)}"})
    for f in BUDGET_METRICS:
        v = limits.get(f)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            errors.append({"name": f"budget:limits.{f}", "status": "fail", "reason": "limit must be a non-negative integer"})
    return errors


def _matches(item: dict, selector: dict | None) -> bool:
    if not selector:
        return True
    for key, patterns in selector.items():
        if patterns in (None, ""):
            continue
        choices = patterns if isinstance(patterns, list) else [patterns]
        if not any(fnmatch.fnmatchcase(str(item.get(key, "")), str(v)) for v in choices):
            return False
    return True


def _limit(checks: list[dict], name: str, actual: int | float | None, rule: str, limit) -> None:
    if limit is None:
        return
    if actual is None:
        checks.append({"name": name, "status": "fail", "rule": rule, "limit": limit,
                       "actual": None, "reason": "metric unavailable"})
        return
    passed = actual == limit if rule == "expected" else actual >= limit if rule == "min" else actual <= limit
    checks.append({"name": name, "status": "pass" if passed else "fail",
                   "rule": rule, "limit": limit, "actual": actual})


def _claim(checks: list[dict], name: str, matched: list[dict],
           claims: dict[str, list[tuple[str, bool]]], allow: bool) -> None:
    overlap = sorted(
        item["id"] for item in matched
        if item["id"] in claims
        and (not allow or any(not prev_allow for _, prev_allow in claims[item["id"]]))
    )
    if overlap:
        checks.append({
            "name": f"{name}:exclusive",
            "status": "fail",
            "overlap": overlap,
            "claimed_by": sorted({owner for item in overlap for owner, _ in claims[item]}),
        })
    for item in matched:
        claims.setdefault(item["id"], []).append((name, allow))


def check_budget(budget: dict, qor: dict) -> dict:
    checks: list[dict] = []
    errors = _shape_errors(budget)

    resources = budget.get("resources") if isinstance(budget.get("resources"), dict) else {}
    res_claims: dict[str, list[tuple[str, bool]]] = {}
    for name, rule in resources.items():
        if not isinstance(rule, dict) or rule.get("operation") not in RESOURCE_OPERATIONS:
            continue
        if _selector_error(rule.get("match"), RESOURCE_MATCH_FIELDS):
            continue
        matched = [
            item for item in qor.get("operator_inventory", [])
            if item.get("operation") == rule["operation"]
            and all(rule.get(k) is None or int(item.get(k, -1)) == int(rule[k])
                    for k in ("a_width", "b_width", "y_width"))
            and _matches(item, rule.get("match"))
        ]
        for field in ("expected", "min", "max"):
            _limit(checks, f"resource:{name}", len(matched), field, rule.get(field))
        if any(rule.get(f) is not None for f in ("expected", "min", "max")):
            _claim(checks, f"resource:{name}", matched, res_claims, bool(rule.get("allow_overlap")))

    storage = budget.get("storage") if isinstance(budget.get("storage"), dict) else {}
    mem_claims: dict[str, list[tuple[str, bool]]] = {}
    for name, rule in storage.items():
        if not isinstance(rule, dict) or _selector_error(rule.get("match"), STORAGE_MATCH_FIELDS):
            continue
        selector = rule.get("match")
        if len(storage) > 1 and not selector:
            checks.append({"name": f"storage:{name}:selector", "status": "fail",
                           "reason": "multiple storage entries require match selectors"})
            continue
        matched = [item for item in qor.get("memory_inventory", []) if _matches(item, selector)]
        _claim(checks, f"storage:{name}", matched, mem_claims, bool(rule.get("allow_overlap")))
        if str(rule.get("implementation") or "").lower() in {"memory", "ram", "sram", "regfile", "register_file"}:
            checks.append({"name": f"storage:{name}:inference", "status": "pass" if matched else "fail",
                           "actual": len(matched)})
        for field in ("width", "depth", "read_ports", "write_ports", "read_latency"):
            if rule.get(field) is None:
                continue
            values = [item.get(field) for item in matched]
            passed = bool(values) and all(v is not None and int(v) == int(rule[field]) for v in values)
            checks.append({"name": f"storage:{name}:{field}", "status": "pass" if passed else "fail",
                           "limit": rule[field], "actual": values})
        if rule.get("banks") is not None:
            _limit(checks, f"storage:{name}:banks", len(matched), "expected", rule["banks"])
        bits = rule.get("bits")
        if bits is None and rule.get("width") is not None and rule.get("depth") is not None:
            bits = int(rule["width"]) * int(rule["depth"]) * int(rule.get("banks") or 1)
        if bits is not None:
            _limit(checks, f"storage:{name}:bits", sum(int(item.get("bits", 0)) for item in matched), "min", bits)

    raw_limits = budget.get("limits") if isinstance(budget.get("limits"), dict) else budget.get("audit")
    limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
    limits.setdefault("latch_bits_max", 0)
    if "comb_loops" in qor:
        limits.setdefault("comb_loops_max", 0)
    for field, (metric, rule) in BUDGET_METRICS.items():
        val = limits.get(field)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            actual = qor.get(metric)
            if actual is None and metric == "ff_bits":
                actual = qor.get("ff_bits_estimate")
            if actual is None and metric == "mux_data_bits":
                actual = qor.get("mux_data_bits_estimate")
            if actual is None and metric == "word_depth":
                actual = qor.get("logic_depth")
            _limit(checks, f"limit:{field}", actual, rule, val)

    failures = errors + [item for item in checks if item["status"] == "fail"]
    return {
        "status": "fail" if failures else "pass",
        "checks": checks,
        "failures": failures,
    }


def metric_map(qor: dict) -> dict[str, int | None]:
    ff = qor.get("ff_bits", qor.get("ff_bits_estimate"))
    mux = qor.get("mux_data_bits", qor.get("mux_data_bits_estimate"))
    word_depth = qor.get("word_depth", qor.get("logic_depth"))
    result: dict[str, int | None] = {
        "ff_bits": ff,
        "latch_bits": qor.get("latch_bits", 0),
        "comb_loops": qor.get("comb_loops", 0),
        "stuck_output_bits": qor.get("stuck_output_bits", 0),
        "undriven_output_bits": qor.get("undriven_output_bits", 0),
        "max_fanout": qor.get("max_fanout", 0),
        "memory_objects": qor.get("memory_objects"),
        "memory_bits": qor.get("memory_bits"),
        "mux_data_bits": mux,
        "gate_depth": qor.get("gate_depth"),
        "word_depth": word_depth,
    }
    for op, count in (qor.get("operator_groups") or {}).items():
        result[op] = count
    return result


def compare_baseline(baseline: dict, current_qor: dict, budget: dict | None = None) -> dict:
    """Compare QoR metrics against baseline without refusing on parameter/config changes."""
    before = metric_map(baseline.get("qor") or baseline.get("hardware") or {})
    after = metric_map(current_qor)

    b_section = (budget or {}).get("limits") or (budget or {}).get("audit") or {}
    raw_limits = b_section.get("regression_pct_max") or {}
    limits: dict[str, float] = {}
    if isinstance(raw_limits, dict):
        for k, v in raw_limits.items():
            canon = METRIC_ALIASES.get(str(k), str(k))
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                limits[canon] = float(v)

    ordered_keys = [
        "ff_bits", "latch_bits", "comb_loops", "stuck_output_bits", "max_fanout",
        "memory_bits", "memory_objects",
        "gate_depth", "word_depth", "mux_data_bits",
        "multiply", "add_sub", "compare", "shift", "divide_mod", "mux",
    ]
    for extra_k in sorted((set(before) | set(after) | set(limits)) - set(ordered_keys)):
        ordered_keys.append(extra_k)

    rows, failures = [], []
    for metric in ordered_keys:
        old, new = before.get(metric), after.get(metric)
        limit = limits.get(metric)
        row: dict = {"metric": metric, "baseline": old, "current": new}
        if old is None or new is None:
            row["status"] = "unavailable"
        else:
            row["delta"] = new - old
            row["delta_pct"] = (0.0 if new == 0 else None) if old == 0 else (new - old) / abs(old) * 100.0
            if limit is not None:
                row["limit_pct"] = limit
                failed = (old == 0 and new > 0) or (row["delta_pct"] is not None and row["delta_pct"] > limit)
                row["status"] = "fail" if failed else "pass"
                if failed:
                    failures.append(row)
            else:
                row["status"] = "info"
        rows.append(row)

    return {
        "status": "fail" if failures else "pass",
        "config_notes": [],
        "metrics": rows,
        "failures": failures,
    }
