"""基线对比引擎与接口测试（TestClient + 临时 SQLite）。"""
import copy
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test_compare.db"

from fastapi.testclient import TestClient  # noqa: E402

from app import compare  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_issue(direction="backward", path="#/properties/a", code="type_removed",
               severity="error", message="m", reason="r"):
    return {
        "direction": direction, "path": path, "code": code,
        "severity": severity, "message": message, "reason": reason,
        "counterexample": None, "counterexample_path": None,
    }


class TestFingerprint:
    def test_stable_regardless_of_message_and_counterexample(self):
        a = make_issue()
        b = make_issue(message="完全不同的 message")
        b["counterexample"] = {"a": 1}
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_reason_whitespace_normalized(self):
        a = make_issue(reason="旧版允许  类型 string\n")
        b = make_issue(reason=" 旧版允许 类型 string ")
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_reason_heuristic_suffix_normalized(self):
        # warning 降级时追加的固定后缀不影响指纹：同一问题只体现为严重级别变化
        suffix = "（未能自动构造出经过验证的反例，标记为潜在风险）"
        warning = make_issue(severity="warning", reason="旧版无此限制" + suffix)
        error = make_issue(severity="error", reason="旧版无此限制")
        assert compare.issue_fingerprint(warning) == compare.issue_fingerprint(error)

    def test_direction_path_code_all_matter(self):
        base = make_issue()
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(direction="forward"))
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(path="#/properties/b"))
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(code="required_added"))
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(reason="另一条原因"))

    def test_fingerprint_format(self):
        fp = compare.issue_fingerprint(make_issue())
        assert isinstance(fp, str) and len(fp) == 16
        int(fp, 16)  # 合法十六进制


class TestCompareReports:
    def test_categories(self):
        issue_a = make_issue(code="required_added", path="#", reason="r1")
        issue_b = make_issue(code="pattern_changed", severity="warning", reason="r2")
        issue_c = make_issue(code="type_removed", reason="r3")
        baseline = {"backward_compatible": False, "forward_compatible": True,
                    "issues": [issue_a, issue_b, issue_c]}
        current = {"backward_compatible": False, "forward_compatible": True,
                   "issues": [dict(issue_a), dict(issue_b, severity="error"),
                              make_issue(code="enum_added", reason="r4")]}

        result = compare.compare_reports(baseline, current)
        groups = result["issues"]
        assert [i["code"] for i in groups["persistent"]] == ["required_added"]
        assert [i["code"] for i in groups["fixed"]] == ["type_removed"]
        assert [i["code"] for i in groups["added"]] == ["enum_added"]
        changed = groups["severity_changed"]
        assert len(changed) == 1
        assert changed[0]["code"] == "pattern_changed"
        assert changed[0]["baseline_severity"] == "warning"
        assert changed[0]["current_severity"] == "error"
        assert result["counts"] == {
            "added": 1, "fixed": 1, "persistent": 1, "severity_changed": 1,
        }

    def test_summary_and_compatibility_delta(self):
        baseline = {"backward_compatible": True, "forward_compatible": True,
                    "issues": [make_issue(severity="warning")]}
        current = {"backward_compatible": False, "forward_compatible": True,
                   "issues": [make_issue(),
                              make_issue(code="x", severity="warning")]}

        result = compare.compare_reports(baseline, current)
        assert result["summary"]["baseline"]["errors"] == 0
        assert result["summary"]["baseline"]["warnings"] == 1
        assert result["summary"]["current"]["errors"] == 1
        assert result["summary"]["delta"]["errors"] == 1
        assert result["summary"]["delta"]["warnings"] == 0
        assert result["summary"]["delta"]["backward"]["errors"] == 1
        assert result["summary"]["delta"]["forward"]["errors"] == 0
        assert result["compatibility"]["backward"] == {
            "baseline": True, "current": False, "change": "regressed",
        }
        assert result["compatibility"]["forward"] == {
            "baseline": True, "current": True, "change": "unchanged",
        }

    def test_duplicate_fingerprints_matched_by_multiplicity(self):
        dup = make_issue()
        baseline = {"issues": [dict(dup), dict(dup)]}
        current = {"issues": [dict(dup)]}
        result = compare.compare_reports(baseline, current)
        assert result["counts"]["persistent"] == 1
        assert result["counts"]["fixed"] == 1
        assert result["counts"]["added"] == 0

    def test_does_not_mutate_reports(self):
        baseline = {"backward_compatible": False, "forward_compatible": True,
                    "issues": [make_issue()]}
        current = {"backward_compatible": True, "forward_compatible": True,
                   "issues": []}
        baseline_snapshot = copy.deepcopy(baseline)
        current_snapshot = copy.deepcopy(current)
        compare.compare_reports(baseline, current)
        assert baseline == baseline_snapshot
        assert current == current_snapshot

    def test_empty_reports(self):
        result = compare.compare_reports({}, {})
        assert result["counts"] == {
            "added": 0, "fixed": 0, "persistent": 0, "severity_changed": 0,
        }
        assert result["summary"]["delta"]["errors"] == 0


# ---------------------------------------------------------------------------
# 接口端到端测试
# ---------------------------------------------------------------------------

OLD = {
    "type": "object",
    "properties": {
        "a": {"type": "string"},
        "b": {"type": "integer"},
    },
    "required": ["a"],
}
# 相对 OLD：b 变为 string（type_removed）、b 变为必填（required_added）
NEW1 = {
    "type": "object",
    "properties": {
        "a": {"type": "string"},
        "b": {"type": "string"},
    },
    "required": ["a", "b"],
}
# 相对 OLD：b 仍为 integer 但新增 minimum（minimum_increased）、b 变为必填
NEW2 = {
    "type": "object",
    "properties": {
        "a": {"type": "string"},
        "b": {"type": "integer", "minimum": 5},
    },
    "required": ["a", "b"],
}


def _create(client, old, new, name=None):
    resp = client.post("/api/analyses",
                       json={"name": name, "old_schema": old, "new_schema": new})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCompareAPI:
    def test_compare_two_analyses(self, client):
        baseline = _create(client, OLD, NEW1, "baseline")
        current = _create(client, OLD, NEW2, "current")

        resp = client.post("/api/analyses/compare", json={
            "baseline_id": baseline["id"], "current_id": current["id"],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["baseline_id"] == baseline["id"]
        assert data["current_id"] == current["id"]

        groups = data["issues"]
        assert [i["code"] for i in groups["persistent"]] == ["required_added"]
        assert [i["code"] for i in groups["added"]] == ["minimum_increased"]
        assert sorted(i["code"] for i in groups["fixed"]) == [
            "type_removed", "type_removed",
        ]
        assert groups["severity_changed"] == []
        assert data["counts"] == {
            "added": 1, "fixed": 2, "persistent": 1, "severity_changed": 0,
        }

        # 指纹与引擎函数一致，且稳定
        baseline_issue = next(
            i for i in baseline["report"]["issues"] if i["code"] == "required_added"
        )
        assert groups["persistent"][0]["fingerprint"] == compare.issue_fingerprint(
            baseline_issue)

        # 双向兼容性变化：forward 由不兼容变为兼容
        assert data["compatibility"]["backward"] == {
            "baseline": False, "current": False, "change": "unchanged",
        }
        assert data["compatibility"]["forward"] == {
            "baseline": False, "current": True, "change": "improved",
        }

        # 错误 / 警告汇总差异（delta = current - baseline）
        assert data["summary"]["baseline"]["errors"] == 3
        assert data["summary"]["baseline"]["backward"]["errors"] == 2
        assert data["summary"]["baseline"]["forward"]["errors"] == 1
        assert data["summary"]["current"]["errors"] == 2
        assert data["summary"]["delta"]["errors"] == -1
        assert data["summary"]["delta"]["warnings"] == 0
        assert data["summary"]["delta"]["backward"]["errors"] == 0
        assert data["summary"]["delta"]["forward"]["errors"] == -1

    def test_compare_same_analysis_is_noop(self, client):
        base = _create(client, OLD, NEW1)
        resp = client.post("/api/analyses/compare", json={
            "baseline_id": base["id"], "current_id": base["id"],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["counts"]["added"] == 0
        assert data["counts"]["fixed"] == 0
        assert data["counts"]["severity_changed"] == 0
        assert data["counts"]["persistent"] == 3
        assert data["summary"]["delta"]["errors"] == 0
        assert data["summary"]["delta"]["total"] == 0
        assert all(c["change"] == "unchanged"
                   for c in data["compatibility"].values())

    def test_compare_missing_analysis_returns_404(self, client):
        base = _create(client, OLD, NEW1)
        resp = client.post("/api/analyses/compare", json={
            "baseline_id": 999999, "current_id": base["id"],
        })
        assert resp.status_code == 404
        resp = client.post("/api/analyses/compare", json={
            "baseline_id": base["id"], "current_id": 999999,
        })
        assert resp.status_code == 404

    def test_compare_does_not_modify_reports(self, client):
        baseline = _create(client, OLD, NEW1)
        current = _create(client, OLD, NEW2)
        before_baseline = client.get(f"/api/analyses/{baseline['id']}").json()
        before_current = client.get(f"/api/analyses/{current['id']}").json()

        resp = client.post("/api/analyses/compare", json={
            "baseline_id": baseline["id"], "current_id": current["id"],
        })
        assert resp.status_code == 200

        assert client.get(f"/api/analyses/{baseline['id']}").json() == before_baseline
        assert client.get(f"/api/analyses/{current['id']}").json() == before_current
