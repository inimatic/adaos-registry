from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_library_agent.storage import StorageAdmission


def policy(tmp_path, free=1000, **kwargs):
    return StorageAdmission(tmp_path / "budget.sqlite3",
                            capacity=lambda path: SimpleNamespace(free=free),
                            quotas={"av": 100, "staging": 100},
                            system_floor=100, media_floor=200, hysteresis=50, **kwargs)


def allocation(tmp_path, size=60, category="av"):
    return [{"path": tmp_path / "output", "bytes": size, "category": category}]


def test_process_shared_budget_and_idempotency(tmp_path):
    first, second = policy(tmp_path), policy(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: pair[0].reserve(pair[1], allocation(tmp_path)),
                                [(first, "a"), (second, "b")]))
    assert sum(item["ok"] for item in results) == 1
    owner = "a" if results[0]["ok"] else "b"
    assert second.reserve(owner, allocation(tmp_path))["reused"] is True
    assert second.reserve(owner, allocation(tmp_path, 61))["ok"] is False


def test_free_floor_hysteresis_unknown_capacity_and_restart(tmp_path):
    denied = policy(tmp_path, 150).reserve("a", allocation(tmp_path))
    assert denied["reason"] == "free_space_floor"
    assert not policy(tmp_path, 190).reserve("a", allocation(tmp_path))["ok"]
    assert policy(tmp_path, 220).reserve("a", allocation(tmp_path))["ok"]
    assert not policy(tmp_path, -1).reserve("b", allocation(tmp_path, 1))["ok"]


def test_staging_and_committed_growth_are_accounted_separately(tmp_path):
    admission = policy(tmp_path)
    assert admission.reserve("a", allocation(tmp_path) + allocation(tmp_path, 80, "staging"))["ok"]
    assert not admission.reserve("b", allocation(tmp_path, 30, "staging"))["ok"]
    admission.commit("a", output_bytes=45)
    assert admission.reserve("b", allocation(tmp_path, 30, "staging"))["ok"]
    assert not policy(tmp_path).reserve("c", allocation(tmp_path))["ok"]
    admission.release("a")
    assert admission.reserve("c", allocation(tmp_path))["ok"]


def test_missing_capacity_fails_closed(tmp_path):
    admission = policy(tmp_path)
    def unavailable(path):
        raise OSError("capacity unavailable")
    admission.capacity = unavailable
    assert admission.reserve("a", allocation(tmp_path))["reason"] == "capacity_unknown"
