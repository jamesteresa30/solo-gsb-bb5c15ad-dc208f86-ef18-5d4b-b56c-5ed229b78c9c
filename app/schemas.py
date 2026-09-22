"""Pydantic 请求 / 响应模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field


class AnalysisCreate(BaseModel):
    """POST /api/analyses 请求体。"""

    name: str | None = Field(default=None, max_length=255, description="可选的分析名称")
    old_schema: dict[str, Any] = Field(..., description="旧版 JSON Schema")
    new_schema: dict[str, Any] = Field(..., description="新版 JSON Schema")
    samples: list[Any] | None = Field(default=None, description="可选的样例文档列表")


class Issue(BaseModel):
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str
    counterexample: Any | None = None
    counterexample_path: str | None = None


class DirectionSummary(BaseModel):
    errors: int
    warnings: int
    total: int


class ReportSummary(BaseModel):
    backward: DirectionSummary
    forward: DirectionSummary
    total: int
    errors: int
    warnings: int


class SampleResult(BaseModel):
    index: int
    valid_against_old: bool
    valid_against_new: bool
    direction: str | None = None


class Report(BaseModel):
    backward_compatible: bool
    forward_compatible: bool
    directions: dict[str, str]
    summary: ReportSummary
    issues: list[Issue]
    sample_results: list[SampleResult]


class AnalysisResponse(BaseModel):
    id: int
    name: str | None
    created_at: str
    old_schema: dict[str, Any]
    new_schema: dict[str, Any]
    samples: list[Any]
    report: Report


class AnalysisListItem(BaseModel):
    id: int
    name: str | None
    created_at: str
    backward_compatible: bool
    forward_compatible: bool
    errors: int
    warnings: int


class AnalysisListResponse(BaseModel):
    total: int
    items: list[AnalysisListItem]


class CompareRequest(BaseModel):
    """POST /api/analyses/compare 请求体。"""

    baseline_id: int = Field(..., ge=1, description="作为基线的分析 ID")
    current_id: int = Field(..., ge=1, description="待对比的当前分析 ID")


class ComparedIssue(BaseModel):
    """对比结果中的单条 issue（附稳定指纹）。"""

    fingerprint: str
    direction: str
    path: str
    code: str
    severity: str
    message: str
    reason: str


class SeverityChangedIssue(BaseModel):
    """仅严重级别发生变化的 issue。"""

    fingerprint: str
    direction: str
    path: str
    code: str
    message: str
    reason: str
    baseline_severity: str
    current_severity: str


class CompatibilityChange(BaseModel):
    """单个兼容方向在两次分析间的变化。"""

    baseline: bool
    current: bool
    change: Literal["unchanged", "improved", "regressed"]


class CompareSummary(BaseModel):
    """错误 / 警告汇总：基线、当前及两者差值（delta = current - baseline）。"""

    baseline: ReportSummary
    current: ReportSummary
    delta: ReportSummary


class CompareCounts(BaseModel):
    added: int
    fixed: int
    persistent: int
    severity_changed: int


class CompareIssueGroups(BaseModel):
    added: list[ComparedIssue]
    fixed: list[ComparedIssue]
    persistent: list[ComparedIssue]
    severity_changed: list[SeverityChangedIssue]


class CompareResponse(BaseModel):
    baseline_id: int
    current_id: int
    compatibility: dict[str, CompatibilityChange]
    summary: CompareSummary
    counts: CompareCounts
    issues: CompareIssueGroups
