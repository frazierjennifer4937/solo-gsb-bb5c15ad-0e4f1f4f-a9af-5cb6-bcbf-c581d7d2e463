"""API 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class DiagnosticOut(BaseModel):
    code: str
    severity: str = Field(description="info | warning | error")
    message: str


class ChangeOut(BaseModel):
    path: str = ""
    tag: Optional[str] = None
    keyword: str
    action: str = Field(description="REPLACE | EMPTY | REMOVE")
    old: Optional[str] = None
    new: Optional[str] = None


class FileOut(BaseModel):
    name: str
    status: str = Field(description="ok | warning | error | skipped")
    output_name: Optional[str] = None
    patient_key: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    max_severity: Optional[str] = None
    diagnostics: list[DiagnosticOut] = []
    changes: list[ChangeOut] = []
    changes_truncated: bool = False


class PatientOut(BaseModel):
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int


class StudyOut(BaseModel):
    old_study_instance_uid: str
    new_study_instance_uid: str
    patient_key: str
    file_count: int


class SeriesOut(BaseModel):
    old_series_instance_uid: str
    new_series_instance_uid: str
    new_study_instance_uid: str
    file_count: int


class JobOut(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    changes_capped: bool
    patients: list[PatientOut]
    studies: list[StudyOut]
    series: list[SeriesOut]
    files: list[FileOut]
    error: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    status: str
    source_zip: str
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    created_at: float
    expires_at: float


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# POST /jobs/consistency-check
# --------------------------------------------------------------------------- #
class ConsistencyCheckIn(BaseModel):
    job_ids: list[str] = Field(
        description="2～5 个已完成任务 ID（不可重复）",
        min_length=2,
        max_length=5,
    )

    @field_validator("job_ids")
    @classmethod
    def _validate_job_ids(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("任务 ID 不能为空")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("任务 ID 不可重复")
        return cleaned


class ConsistencyJobSummary(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    patient_count: int
    study_count: int
    series_count: int
    sop_count: int


class PatientMappingValue(BaseModel):
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int


class PatientMappingConflict(BaseModel):
    patient_key: str = Field(description="跨任务共同的原始患者归组键（如 id:P-001）")
    differing_fields: list[str] = Field(
        description="取值不一致的字段：new_patient_id | new_patient_name | date_shift_days"
    )
    mappings: dict[str, PatientMappingValue] = Field(
        description="按任务 ID 列出的伪名/日期偏移"
    )


class PatientMappingOnly(BaseModel):
    patient_key: str
    job_id: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int


class PatientMappingDiff(BaseModel):
    shared_patient_count: int = Field(
        description="至少在两个任务中出现的患者键数量"
    )
    conflict_count: int
    only_in_one_job_count: int
    conflicts: list[PatientMappingConflict]
    only_in_one_job: list[PatientMappingOnly]


class UidMappingValue(BaseModel):
    new_uid: str


class UidMappingConflict(BaseModel):
    old_uid: str
    mappings: dict[str, UidMappingValue] = Field(
        description="按任务 ID 列出的新 UID（冲突任务新 UID 不一致）"
    )


class UidMappingOnly(BaseModel):
    old_uid: str
    job_id: str
    new_uid: str


class UidLevelDiff(BaseModel):
    level: str = Field(description="study | series | sop")
    shared_uid_count: int = Field(description="至少在两个任务中出现的原始 UID 数量")
    conflict_count: int
    only_in_one_job_count: int
    conflicts: list[UidMappingConflict]
    only_in_one_job: list[UidMappingOnly]


class UidMappingDiff(BaseModel):
    study: UidLevelDiff
    series: UidLevelDiff
    sop: UidLevelDiff


class ConsistencyCheckOut(BaseModel):
    overall_status: str = Field(
        description=(
            "consistent（同一密钥指纹且共享患者/UID 映射全部一致）"
            " | conflicts（存在伪名或 UID 映射差异）"
        )
    )
    secret_fingerprint: str = Field(
        description="参与任务共用的密钥 SHA-256 指纹前 12 位"
    )
    job_count: int
    jobs: list[ConsistencyJobSummary]
    patient_diff: PatientMappingDiff
    uid_diff: UidMappingDiff
