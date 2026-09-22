"""POST /jobs/consistency-check：跨任务密钥指纹 / 伪名 / UID 映射核对。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import consistency
from tests.sample_data import build_sample_zip


SECRET_A = "consistency-secret-aaa"
SECRET_B = "consistency-secret-bbb"
# 对 id:P-001 / id:P-002 均派生出非零日期偏移（109 / -195）
SHIFT_SECRET = "shift-check-secret-000"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


@pytest.fixture()
def zips(tmp_path):
    p1 = tmp_path / "s1.zip"
    p2 = tmp_path / "s2.zip"
    build_sample_zip(str(p1), seed=1)
    build_sample_zip(str(p2), seed=2)
    return p1.read_bytes(), p2.read_bytes()


def _upload(client, data: bytes, secret: str = SECRET_A, shift_dates: str = "true"):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", data, "application/zip")},
        data={"secret": secret, "shift_dates": shift_dates},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _check(client, job_ids):
    return client.post("/jobs/consistency-check", json={"job_ids": job_ids})


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_same_batch_twice_is_consistent(client, zips):
    j1 = _upload(client, zips[0])
    j2 = _upload(client, zips[0])
    r = _check(client, [j1["id"], j2["id"]])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall_status"] == "consistent"
    assert body["secret_fingerprint"] == j1["secret_fingerprint"]
    assert body["job_count"] == 2
    assert {j["id"] for j in body["jobs"]} == {j1["id"], j2["id"]}

    pd = body["patient_diff"]
    assert pd["shared_patient_count"] == 2
    assert pd["conflict_count"] == 0
    assert pd["only_in_one_job_count"] == 0
    assert pd["conflicts"] == []

    for level in ("study", "series", "sop"):
        d = body["uid_diff"][level]
        assert d["conflict_count"] == 0
        assert d["only_in_one_job_count"] == 0
        assert d["shared_uid_count"] >= 1


def test_disjoint_batches_are_consistent_with_only_in_one_job(client, zips):
    """不同 ZIP（不同 seed，UID 完全不同）共享患者键但 UID 不重叠。"""
    j1 = _upload(client, zips[0])
    j2 = _upload(client, zips[1])
    body = _check(client, [j1["id"], j2["id"]]).json()
    assert body["overall_status"] == "consistent"

    # 患者 id:P-001 / id:P-002 在两个任务中都出现，伪名相同
    pd = body["patient_diff"]
    assert pd["shared_patient_count"] == 2
    assert pd["only_in_one_job_count"] == 0

    # UID 全部只在单个任务中出现，且不构成冲突
    for level in ("study", "series", "sop"):
        d = body["uid_diff"][level]
        assert d["conflict_count"] == 0
        assert d["shared_uid_count"] == 0
        assert d["only_in_one_job_count"] >= 1


def test_same_patient_pseudonyms_match_across_batches(client, zips):
    j1 = _upload(client, zips[0])
    j2 = _upload(client, zips[1])
    body = _check(client, [j2["id"], j1["id"]]).json()
    p1 = {p["key"]: p for p in j1["patients"]}
    p2 = {p["key"]: p for p in j2["patients"]}
    assert body["patient_diff"]["conflicts"] == []
    # 接口里的共享患者数量与任务患者数一致
    assert body["patient_diff"]["shared_patient_count"] == len(p1) == len(p2)
    # 伪名确实一致（抽样）
    assert p1["id:P-001"]["new_patient_id"] == p2["id:P-001"]["new_patient_id"]


def test_job_summary_counts(client, zips):
    j1 = _upload(client, zips[0])
    j2 = _upload(client, zips[0])
    body = _check(client, [j1["id"], j2["id"]]).json()
    summaries = {j["id"]: j for j in body["jobs"]}
    for job_id, job in ((j1["id"], j1), (j2["id"], j2)):
        s = summaries[job_id]
        assert s["status"] == "ready"
        assert s["file_count"] == job["file_count"] == 6
        assert s["output_file_count"] == 4
        assert s["error_count"] == 2
        assert s["patient_count"] == 2
        assert s["shift_dates"] is True


def test_shift_dates_flag_difference_creates_patient_conflict(client, zips):
    j1 = _upload(client, zips[0], secret=SHIFT_SECRET, shift_dates="true")
    j2 = _upload(client, zips[0], secret=SHIFT_SECRET, shift_dates="false")
    body = _check(client, [j1["id"], j2["id"]]).json()
    assert body["overall_status"] == "conflicts"
    pd = body["patient_diff"]
    assert pd["conflict_count"] == 2
    conflict = next(c for c in pd["conflicts"] if c["patient_key"] == "id:P-001")
    assert conflict["differing_fields"] == ["date_shift_days"]
    shifts = {jid: m["date_shift_days"] for jid, m in conflict["mappings"].items()}
    assert shifts == {j1["id"]: 109, j2["id"]: 0}
    # 伪名本身不受 shift_dates 影响
    for m in conflict["mappings"].values():
        assert m["new_patient_id"]
    # UID 映射与日期平移无关，仍一致
    for level in ("study", "series", "sop"):
        assert body["uid_diff"][level]["conflict_count"] == 0


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #
def test_different_secret_fingerprint_conflicts(client, zips):
    j1 = _upload(client, zips[0], secret=SECRET_A)
    j2 = _upload(client, zips[0], secret=SECRET_B)
    r = _check(client, [j1["id"], j2["id"]])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "SECRET_FINGERPRINT_MISMATCH"
    fps = {f["job_id"]: f["secret_fingerprint"] for f in detail["fingerprints"]}
    assert fps == {
        j1["id"]: j1["secret_fingerprint"],
        j2["id"]: j2["secret_fingerprint"],
    }
    assert j1["secret_fingerprint"] != j2["secret_fingerprint"]


def test_missing_job_returns_404(client, zips):
    j1 = _upload(client, zips[0])
    r = _check(client, [j1["id"], "deadbeefdeadbeef"])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "JOB_NOT_FOUND"
    assert detail["job_id"] == "deadbeefdeadbeef"


def test_failed_job_returns_409(client, tmp_path):
    failed = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "consistency-secret-aaa"},
    ).json()
    assert failed["status"] == "failed"

    good_zip = tmp_path / "good.zip"
    build_sample_zip(str(good_zip))
    good = _upload(client, good_zip.read_bytes())

    r = _check(client, [good["id"], failed["id"]])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "JOB_NOT_READY"
    assert detail["job_id"] == failed["id"]
    assert detail["status"] == "failed"


@pytest.mark.parametrize("job_ids", [
    ["only-one"],
    ["a", "b", "c", "d", "e", "f"],
    ["a", "a"],
    ["", "b"],
])
def test_invalid_payload_returns_422(client, job_ids):
    r = client.post("/jobs/consistency-check", json={"job_ids": job_ids})
    assert r.status_code == 422


def test_missing_body_returns_422(client):
    assert client.post("/jobs/consistency-check").status_code == 422


# --------------------------------------------------------------------------- #
# 比对逻辑单元测试：同一密钥下不会自然出现的伪名/UID 分叉
# （派生版本漂移、数据被外部篡改等场景）
# --------------------------------------------------------------------------- #
def _stub_job(job_id: str, patients=None, studies=None, series=None, sops=None):
    result = SimpleNamespace(
        files=[],
        patients=patients or {},
        studies=studies or {},
        series=series or {},
        sops=sops or {},
    )
    return SimpleNamespace(
        id=job_id,
        status="ready",
        source_name="x.zip",
        secret_fingerprint="fp1234567890",
        shift_dates=True,
        created_at=0.0,
        expires_at=3600.0,
        ttl=3600,
        result=result,
    )


def _patient(key, pid, name, shift):
    return SimpleNamespace(
        key=key, new_patient_id=pid, new_patient_name=name, date_shift_days=shift
    )


def test_unit_patient_id_conflict_detected():
    j1 = _stub_job("j1", patients={
        "id:P-001": _patient("id:P-001", "AAAA", "ANON^AAAA", 5),
    })
    j2 = _stub_job("j2", patients={
        "id:P-001": _patient("id:P-001", "BBBB", "ANON^BBBB", 5),
    })
    report = consistency.build_report([j1, j2])
    assert report.overall_status == "conflicts"
    c = report.patient_diff.conflicts[0]
    assert c.patient_key == "id:P-001"
    assert set(c.differing_fields) == {"new_patient_id", "new_patient_name"}
    assert c.mappings["j1"].new_patient_id == "AAAA"
    assert c.mappings["j2"].new_patient_id == "BBBB"


def test_unit_uid_conflict_detected_for_every_level():
    j1 = _stub_job(
        "j1",
        studies={"1.1": {"old_study_instance_uid": "1.1",
                         "new_study_instance_uid": "2.25.1"}},
        series={"1.2": {"old_series_instance_uid": "1.2",
                        "new_series_instance_uid": "2.25.2",
                        "new_study_instance_uid": "2.25.1"}},
        sops={"1.3": "2.25.3"},
    )
    j2 = _stub_job(
        "j2",
        studies={"1.1": {"old_study_instance_uid": "1.1",
                         "new_study_instance_uid": "2.25.9"}},
        series={"1.2": {"old_series_instance_uid": "1.2",
                        "new_series_instance_uid": "2.25.8",
                        "new_study_instance_uid": "2.25.9"}},
        sops={"1.3": "2.25.7"},
    )
    report = consistency.build_report([j1, j2])
    assert report.overall_status == "conflicts"
    for level in ("study", "series", "sop"):
        d = getattr(report.uid_diff, level)
        assert d.shared_uid_count == 1
        assert d.conflict_count == 1
        assert d.conflicts[0].old_uid == {"study": "1.1",
                                          "series": "1.2",
                                          "sop": "1.3"}[level]


def test_unit_synthetic_entries_are_not_compared():
    """自映射 SOP 与缺失原始 UID 的 study/series 补全条目不应参与比对。"""
    synth_sop = "2.25.synthetic"
    j1 = _stub_job(
        "j1",
        studies={"ns": {"old_study_instance_uid": "",
                        "new_study_instance_uid": "2.25.s1"}},
        series={"ns": {"old_series_instance_uid": "",
                       "new_series_instance_uid": "2.25.s2",
                       "new_study_instance_uid": "2.25.s1"}},
        sops={synth_sop: synth_sop},
    )
    j2 = _stub_job(
        "j2",
        studies={"ns": {"old_study_instance_uid": "",
                        "new_study_instance_uid": "2.25.other-s1"}},
        series={"ns": {"old_series_instance_uid": "",
                       "new_series_instance_uid": "2.25.other-s2",
                       "new_study_instance_uid": "2.25.other-s1"}},
        sops={synth_sop: synth_sop},
    )
    report = consistency.build_report([j1, j2])
    assert report.overall_status == "consistent"
    for level in ("study", "series", "sop"):
        d = getattr(report.uid_diff, level)
        assert d.shared_uid_count == 0
        assert d.conflict_count == 0
        assert d.only_in_one_job_count == 0
