"""跨任务一致性核对。

同一把稳定密钥下，确定性派生保证“同一原始标识 → 同一伪名/UID”。
本模块对 2～5 个已完成（ready）任务做离线核对：

* 患者稳定伪名：以任务内的患者归组键（``id:<PatientID>`` / ``name:<...>`` …）
  为基准，比较共享患者的 ``new_patient_id`` / ``new_patient_name`` /
  ``date_shift_days``；
* Study / Series / SOP UID 映射：以原始 UID 为基准，比较新 UID 是否一致。

仅出现在单个任务中的患者/UID 不属于冲突（批次间内容本可不同），但会汇总到
``only_in_one_job`` 便于审计。为防止响应过大，每类差异列表最多保留
``MAX_DIFF_ITEMS`` 条，总数与截断标志单独给出。
"""
from __future__ import annotations

from typing import Iterable

from .jobs import Job
from .schemas import (
    ConsistencyCheckOut,
    ConsistencyJobSummary,
    PatientMappingConflict,
    PatientMappingDiff,
    PatientMappingOnly,
    PatientMappingValue,
    UidLevelDiff,
    UidMappingConflict,
    UidMappingDiff,
    UidMappingOnly,
    UidMappingValue,
)

MAX_DIFF_ITEMS = 500


# --------------------------------------------------------------------------- #
# 任务摘要
# --------------------------------------------------------------------------- #
def summarize_job(job: Job) -> ConsistencyJobSummary:
    result = job.result
    files = result.files
    return ConsistencyJobSummary(
        id=job.id,
        status=job.status,
        secret_fingerprint=job.secret_fingerprint,
        shift_dates=job.shift_dates,
        source_zip=job.source_name,
        created_at=job.created_at,
        expires_at=job.expires_at,
        file_count=len(files),
        output_file_count=sum(bool(r.output_name) for r in files),
        error_count=sum(r.status == "error" for r in files),
        warning_count=sum(r.status == "warning" for r in files),
        patient_count=len(result.patients),
        study_count=len(result.studies),
        series_count=len(result.series),
        sop_count=len(result.sops),
    )


# --------------------------------------------------------------------------- #
# 患者映射比对
# --------------------------------------------------------------------------- #
def _patient_maps(jobs: Iterable[Job]) -> dict[str, dict[str, tuple]]:
    """返回 patient_key -> {job_id: (new_id, new_name, shift_days)}。"""
    maps: dict[str, dict[str, tuple]] = {}
    for job in jobs:
        for key, info in job.result.patients.items():
            maps.setdefault(key, {})[job.id] = (
                info.new_patient_id,
                info.new_patient_name,
                info.date_shift_days,
            )
    return maps


def _compare_patients(jobs: list[Job]) -> PatientMappingDiff:
    per_job = _patient_maps(jobs)
    conflicts: list[PatientMappingConflict] = []
    only: list[PatientMappingOnly] = []
    shared_count = 0

    for key in sorted(per_job):
        entries = per_job[key]
        if len(entries) == 1:
            job_id, (new_id, new_name, shift) = next(iter(entries.items()))
            only.append(
                PatientMappingOnly(
                    patient_key=key,
                    job_id=job_id,
                    new_patient_id=new_id,
                    new_patient_name=new_name,
                    date_shift_days=shift,
                )
            )
            continue

        shared_count += 1
        differing: set[str] = set()
        reference = next(iter(entries.values()))
        for new_id, new_name, shift in entries.values():
            if new_id != reference[0]:
                differing.add("new_patient_id")
            if new_name != reference[1]:
                differing.add("new_patient_name")
            if shift != reference[2]:
                differing.add("date_shift_days")
        if differing:
            conflicts.append(
                PatientMappingConflict(
                    patient_key=key,
                    differing_fields=sorted(differing),
                    mappings={
                        job_id: PatientMappingValue(
                            new_patient_id=new_id,
                            new_patient_name=new_name,
                            date_shift_days=shift,
                        )
                        for job_id, (new_id, new_name, shift) in entries.items()
                    },
                )
            )

    return PatientMappingDiff(
        shared_patient_count=shared_count,
        conflict_count=len(conflicts),
        only_in_one_job_count=len(only),
        conflicts=conflicts[:MAX_DIFF_ITEMS],
        only_in_one_job=only[:MAX_DIFF_ITEMS],
    )


# --------------------------------------------------------------------------- #
# UID 映射比对
# --------------------------------------------------------------------------- #
def _uid_maps(
    jobs: list[Job],
    level: str,
) -> dict[str, dict[str, str]]:
    """返回 old_uid -> {job_id: new_uid}。

    * study/series 以结果汇总表为准（跳过缺失原始 UID 的确定性补全条目，
      它们的 old UID 在不同任务间不具可比性）；
    * SOP 直接使用首遍扫描建立的 orig -> new 映射；补 UID 场景会把
      新 UID 自身也登记进映射，这种自映射条目同样跳过。
    """
    maps: dict[str, dict[str, str]] = {}
    for job in jobs:
        result = job.result
        if level == "sop":
            iterator = (
                (old, new) for old, new in result.sops.items() if old != new
            )
        elif level == "study":
            iterator = (
                (v["old_study_instance_uid"], v["new_study_instance_uid"])
                for v in result.studies.values()
                if v["old_study_instance_uid"]
            )
        else:
            iterator = (
                (v["old_series_instance_uid"], v["new_series_instance_uid"])
                for v in result.series.values()
                if v["old_series_instance_uid"]
            )
        for old_uid, new_uid in iterator:
            maps.setdefault(old_uid, {})[job.id] = new_uid
    return maps


def _compare_uid_level(jobs: list[Job], level: str) -> UidLevelDiff:
    per_job = _uid_maps(jobs, level)
    conflicts: list[UidMappingConflict] = []
    only: list[UidMappingOnly] = []
    shared_count = 0

    for old_uid in sorted(per_job):
        entries = per_job[old_uid]
        if len(entries) == 1:
            job_id, new_uid = next(iter(entries.items()))
            only.append(
                UidMappingOnly(old_uid=old_uid, job_id=job_id, new_uid=new_uid)
            )
            continue
        shared_count += 1
        distinct = set(entries.values())
        if len(distinct) > 1:
            conflicts.append(
                UidMappingConflict(
                    old_uid=old_uid,
                    mappings={
                        job_id: UidMappingValue(new_uid=new_uid)
                        for job_id, new_uid in entries.items()
                    },
                )
            )

    return UidLevelDiff(
        level=level,
        shared_uid_count=shared_count,
        conflict_count=len(conflicts),
        only_in_one_job_count=len(only),
        conflicts=conflicts[:MAX_DIFF_ITEMS],
        only_in_one_job=only[:MAX_DIFF_ITEMS],
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_report(jobs: list[Job]) -> ConsistencyCheckOut:
    """对一组已通过预检（存在、ready、同指纹）的任务构建核对报告。"""
    patient_diff = _compare_patients(jobs)
    uid_diff = UidMappingDiff(
        study=_compare_uid_level(jobs, "study"),
        series=_compare_uid_level(jobs, "series"),
        sop=_compare_uid_level(jobs, "sop"),
    )
    has_conflicts = (
        patient_diff.conflict_count
        + uid_diff.study.conflict_count
        + uid_diff.series.conflict_count
        + uid_diff.sop.conflict_count
    ) > 0
    return ConsistencyCheckOut(
        overall_status="conflicts" if has_conflicts else "consistent",
        secret_fingerprint=jobs[0].secret_fingerprint,
        job_count=len(jobs),
        jobs=[summarize_job(job) for job in jobs],
        patient_diff=patient_diff,
        uid_diff=uid_diff,
    )
