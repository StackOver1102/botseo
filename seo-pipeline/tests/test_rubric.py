import itertools

import pytest

from vnseo.rubric import CodeChecks, JobInfo, _score, Item, aggregate


def verdict(pass_id, ids, no=(), severity="trung_binh"):
    return {"pass_id": pass_id, "blocking_fail": False, "items": [
        {"check_id": i, "evidence": "", "verdict": "NO" if i in no else "YES",
         "severity": severity, "fix_hint": "", "location": "S01"} for i in ids]}


def full(no=(), severity="trung_binh", skip=()):
    v = {
        "P8a": verdict("P8a", ["P8a.1", "P8a.2", "P8a.3", "P8a.4"], no, severity),
        "P8b": verdict("P8b", [f"P8b.V{i}" for i in range(1, 11)] + ["P8b.B1", "P8b.B2"], no, severity),
        "P8c": verdict("P8c", [f"P8c.{i}" for i in range(1, 6)], no, severity),
        "P8d": verdict("P8d", ["P8d.1", "P8d.2", "P8d.3", "P8d.4"], no, severity),
    }
    return {k: x for k, x in v.items() if k not in skip}


STD = JobInfo("A", "standard", False)


def test_all_yes_passes():
    r = aggregate(full(), CodeChecks(), STD)
    assert r.pass_ and r.total == 18 and r.max_total == 18


def test_threshold_14_of_18():
    # 4 tiêu chí không blocking xuống 1 điểm → 14/18 đạt
    r = aggregate(full(no=["P8b.V1", "P8c.3", "P8c.2", "P8b.B2"]), CodeChecks(), STD)
    assert (r.total, r.pass_) == (14, True)
    r = aggregate(full(no=["P8b.V1", "P8c.3", "P8c.2", "P8b.B2", "P8a.3"]), CodeChecks(), STD)
    assert (r.total, r.pass_) == (13, False)


def test_blocking_zero_fails_even_with_high_ratio():
    r = aggregate(full(no=["P8c.1"], severity="chan"), CodeChecks(), STD)
    assert not r.pass_ and r.blocking_zero == ["intent"]


def test_number_override_forces_accuracy_zero():
    r = aggregate(full(), CodeChecks(number_override_fail=True), STD)
    assert "accuracy" in r.blocking_zero and not r.pass_


def test_lite_voice_is_na():
    r = aggregate(full(skip=("P8b",)), CodeChecks(), JobInfo("A", "lite", False))
    voice = next(s for s in r.scores if s.criterion == "voice")
    assert voice.score is None and r.max_total == 16 and r.pass_


def test_branch_b_containment_blocks():
    r = aggregate(full(), CodeChecks(containment=0.12), JobInfo("B", "standard", False))
    assert "uniqueness" in r.blocking_zero


def test_missing_check_raises():
    v = full()
    v["P8c"]["items"].pop(0)
    with pytest.raises(ValueError):
        aggregate(v, CodeChecks(), STD)


@pytest.mark.parametrize("sev_a,sev_b", list(itertools.product(["chan", "cao", "trung_binh", "thap"], repeat=2)))
def test_score_combinations(sev_a, sev_b):
    s = _score([Item("a", "NO", sev_a), Item("b", "NO", sev_b)])
    assert s == (0 if {sev_a, sev_b} & {"chan", "cao"} else 1)
