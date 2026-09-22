"""分析报告基线对比。

为两次分析（基线 vs 当前）的 issue 列表生成**稳定指纹**，据此把问题归类为：

* ``added``：当前报告新出现的问题；
* ``fixed``：基线中存在、当前已修复的问题；
* ``persistent``：两份报告都存在且严重级别不变的问题；
* ``severity_changed``：同一问题仅严重级别变化（如 warning -> error）。

指纹由 ``方向 + path + code + 规范化原因`` 计算（SHA-256 前 16 位十六进制），
不包含 message、反例、严重级别等易变字段，因此同一引擎版本对同一问题
生成的指纹是稳定的。原因文本的规范化规则：

* 折叠连续空白为单个空格，并去除首尾空白；
* 移除启发式问题降级为 warning 时追加的固定后缀
  ``（未能自动构造出经过验证的反例，标记为潜在风险）``，
  使同一启发式问题在 warning / error 之间只体现为严重级别变化，
  而不是"修复一条 + 新增一条"。

整个对比过程只读取报告内容，不会修改或回写任何原始报告。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

DIRECTIONS = ("backward", "forward")

# 与 compat.IssueBuilder.add 中 heuristic 降级分支追加的后缀保持一致
_HEURISTIC_SUFFIX = "（未能自动构造出经过验证的反例，标记为潜在风险）"
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_reason(reason: Any) -> str:
    """规范化原因文本，使同一问题的指纹不受空白与严重级别后缀影响。"""
    if not isinstance(reason, str):
        return ""
    text = reason
    if text.endswith(_HEURISTIC_SUFFIX):
        text = text[: -len(_HEURISTIC_SUFFIX)]
    return _WHITESPACE_RE.sub(" ", text).strip()


def issue_fingerprint(issue: dict) -> str:
    """按 方向 / path / code / 规范化原因 生成稳定指纹（SHA-256 前 16 位）。"""
    canonical = "\x1f".join((
        str(issue.get("direction") or ""),
        str(issue.get("path") or ""),
        str(issue.get("code") or ""),
        normalize_reason(issue.get("reason")),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _public_issue(issue: dict, fingerprint: str) -> dict:
    """提取对比结果中返回的字段（新建 dict，不触碰原报告）。"""
    return {
        "fingerprint": fingerprint,
        "direction": issue.get("direction") or "",
        "path": issue.get("path") or "",
        "code": issue.get("code") or "",
        "message": issue.get("message") or "",
        "reason": issue.get("reason") or "",
    }


def _sort_key(entry: dict) -> tuple:
    return (entry["direction"], entry["path"], entry["code"], entry["fingerprint"])


def _direction_summary(issues: list[dict]) -> dict:
    return {
        "errors": sum(1 for i in issues if i.get("severity") == "error"),
        "warnings": sum(1 for i in issues if i.get("severity") == "warning"),
        "total": len(issues),
    }


def _report_summary(issues: list[dict]) -> dict:
    """从 issue 列表重新汇总（与报告内 summary 同构），保证对比自洽。"""
    summary = {
        direction: _direction_summary(
            [i for i in issues if i.get("direction") == direction]
        )
        for direction in DIRECTIONS
    }
    summary["total"] = len(issues)
    summary["errors"] = sum(1 for i in issues if i.get("severity") == "error")
    summary["warnings"] = sum(1 for i in issues if i.get("severity") == "warning")
    return summary


def _delta_summary(baseline: dict, current: dict) -> dict:
    delta = {
        direction: {
            key: current[direction][key] - baseline[direction][key]
            for key in ("errors", "warnings", "total")
        }
        for direction in DIRECTIONS
    }
    for key in ("total", "errors", "warnings"):
        delta[key] = current[key] - baseline[key]
    return delta


def _compatibility_change(before: Any, after: Any) -> dict:
    before, after = bool(before), bool(after)
    if before == after:
        change = "unchanged"
    elif after:
        change = "improved"
    else:
        change = "regressed"
    return {"baseline": before, "current": after, "change": change}


def compare_reports(baseline_report: dict, current_report: dict) -> dict:
    """对比两份分析报告，返回归类后的 issue 差异与汇总差异（只读）。"""
    baseline_report = baseline_report or {}
    current_report = current_report or {}
    baseline_issues = list(baseline_report.get("issues") or [])
    current_issues = list(current_report.get("issues") or [])

    # fingerprint -> [issue, ...]，保留同一报告内重复 issue 的多重性
    baseline_map: dict[str, list[dict]] = {}
    for issue in baseline_issues:
        baseline_map.setdefault(issue_fingerprint(issue), []).append(issue)
    current_map: dict[str, list[dict]] = {}
    for issue in current_issues:
        current_map.setdefault(issue_fingerprint(issue), []).append(issue)

    added: list[dict] = []
    fixed: list[dict] = []
    persistent: list[dict] = []
    severity_changed: list[dict] = []

    for fingerprint in sorted(set(baseline_map) | set(current_map)):
        base_group = baseline_map.get(fingerprint, [])
        curr_group = current_map.get(fingerprint, [])
        for base_issue, curr_issue in zip(base_group, curr_group):
            entry = _public_issue(curr_issue, fingerprint)
            if base_issue.get("severity") == curr_issue.get("severity"):
                persistent.append({**entry, "severity": curr_issue.get("severity") or ""})
            else:
                severity_changed.append({
                    **entry,
                    "baseline_severity": base_issue.get("severity") or "",
                    "current_severity": curr_issue.get("severity") or "",
                })
        # 数量多出来的一侧：当前多出 = 新增；基线多出 = 已修复
        for issue in curr_group[len(base_group):]:
            added.append({**_public_issue(issue, fingerprint),
                          "severity": issue.get("severity") or ""})
        for issue in base_group[len(curr_group):]:
            fixed.append({**_public_issue(issue, fingerprint),
                          "severity": issue.get("severity") or ""})

    added.sort(key=_sort_key)
    fixed.sort(key=_sort_key)
    persistent.sort(key=_sort_key)
    severity_changed.sort(key=_sort_key)

    baseline_summary = _report_summary(baseline_issues)
    current_summary = _report_summary(current_issues)

    return {
        "compatibility": {
            direction: _compatibility_change(
                baseline_report.get(f"{direction}_compatible"),
                current_report.get(f"{direction}_compatible"),
            )
            for direction in DIRECTIONS
        },
        "summary": {
            "baseline": baseline_summary,
            "current": current_summary,
            "delta": _delta_summary(baseline_summary, current_summary),
        },
        "counts": {
            "added": len(added),
            "fixed": len(fixed),
            "persistent": len(persistent),
            "severity_changed": len(severity_changed),
        },
        "issues": {
            "added": added,
            "fixed": fixed,
            "persistent": persistent,
            "severity_changed": severity_changed,
        },
    }
