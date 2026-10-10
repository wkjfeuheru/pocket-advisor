"""阈值门禁：gates 组决定退出码，watch 组输出趋势报告。

用法::

    python -m evals.runners.check_baseline                 # 读 summary.json
    python -m evals.runners.check_baseline --summary xx.json

- ``gates``：全部通过退出码 0，任一退化退出码 1（CI 阻断）。
- ``watch``：与上次留档的 live summary 对比，输出涨跌趋势，不决定退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASELINES_PATH = Path(__file__).resolve().parent.parent / "baselines" / "thresholds.json"
DEFAULT_SUMMARY = Path(__file__).resolve().parent.parent.parent / ".cache" / "evals" / "results" / "summary.json"
LAST_LIVE_SUMMARY = Path(__file__).resolve().parent.parent.parent / ".cache" / "evals" / "results" / "last_live_summary.json"


def _flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
            # 别名：section.summary.metric -> section.metric，与 thresholds.json 的写法对齐
            if ".summary." in path:
                flat[path.replace(".summary.", ".", 1)] = value
    return flat


def _metric_value(flat: dict[str, Any], metric: str) -> Any:
    if metric in flat:
        return flat[metric]
    # 允许缩写：summary 段内的相对路径（如 intent_routing.routing_accuracy）
    suffix = "." + metric
    matches = [key for key in flat if key.endswith(suffix) or key == f"{metric}"]
    if len(matches) == 1:
        return flat[matches[0]]
    if matches:
        raise KeyError(f"指标 {metric} 命中多条路径: {matches}")
    raise KeyError(f"summary 中找不到指标 {metric}")


def _compare(value: Any, operator: str, threshold: Any) -> bool:
    if operator == ">=":
        return value >= threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    raise ValueError(f"不支持的比较符: {operator}")


def main() -> int:
    parser = argparse.ArgumentParser(description="阈值门禁")
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY), help="summary.json 路径")
    parser.add_argument("--thresholds", default=str(BASELINES_PATH), help="thresholds.json 路径")
    args = parser.parse_args()

    summary_path = Path(args.summary)
    if not summary_path.exists():
        print(f"FAIL: summary 不存在: {summary_path}（先运行 python -m evals.runners.run_all）")
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    thresholds = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
    flat = _flatten(summary)
    exit_code = 0

    print("== gates（CI 阻断）==")
    for rule in thresholds.get("gates", []):
        metric, operator, threshold = rule["metric"], rule["operator"], rule["value"]
        label = rule.get("label", metric)
        if metric not in flat and not any(key.endswith("." + metric) for key in flat):
            print(f"  MISS  {label}: summary 中无指标 {metric}")
            exit_code = 1
            continue
        try:
            value = _metric_value(flat, metric)
        except KeyError as exc:
            print(f"  MISS  {label}: {exc}")
            exit_code = 1
            continue
        if value is None:
            print(f"  SKIP  {label}: 指标为空（上游 SKIP）")
            continue
        if _compare(value, operator, threshold):
            print(f"  PASS  {label}: {value} {operator} {threshold}")
        else:
            print(f"  FAIL  {label}: {value} {operator} {threshold}")
            exit_code = 1

    print("== watch（趋势观察，不阻断）==")
    watch_rules = thresholds.get("watch", [])
    previous: dict[str, Any] = {}
    if LAST_LIVE_SUMMARY.exists():
        try:
            previous = _flatten(json.loads(LAST_LIVE_SUMMARY.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            previous = {}
    for rule in watch_rules:
        metric, operator, threshold = rule["metric"], rule["operator"], rule["value"]
        label = rule.get("label", metric)
        section = metric.split(".", 1)[0]
        section_data = summary.get(section)
        if section not in summary:
            print(f"  SKIP  {label}: 未运行（离线模式无 live 指标）")
            continue
        if isinstance(section_data, dict) and section_data.get("status") == "SKIP":
            print(f"  SKIP  {label}: runner 跳过（{section_data.get('reason', '未配置')}）")
            continue
        if isinstance(section_data, dict) and section_data.get("status") == "ERROR":
            print(f"  ERROR {label}: runner 崩溃（{section_data.get('reason', '')}）")
            continue
        try:
            value = _metric_value(flat, metric)
        except KeyError as exc:
            print(f"  MISS  {label}: {exc}")
            continue
        if value is None:
            print(f"  SKIP  {label}: 指标为空（上游 SKIP）")
            continue
        trend = ""
        if metric in previous and isinstance(value, (int, float)):
            delta = round(value - previous[metric], 4)
            trend = f"（上次 {previous[metric]}，{'+' if delta >= 0 else ''}{delta}）"
        status = "PASS" if _compare(value, operator, threshold) else "WATCH"
        print(f"  {status}  {label}: {value} {operator} {threshold}{trend}")

    if summary.get("meta", {}).get("mode") == "live":
        LAST_LIVE_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                     encoding="utf-8")
        print(f"live 基线已留档: {LAST_LIVE_SUMMARY}")

    print("RESULT:", "PASS" if exit_code == 0 else "FAIL")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
