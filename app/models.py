"""Pydantic 请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, model_validator


class CreateBarrierRequest(BaseModel):
    participants: list[str] = Field(
        ..., min_length=1, description="屏障参与者标识名单（如主机名、任务 id）"
    )
    threshold: int | None = Field(
        default=None,
        ge=1,
        description="达标所需报到人数；缺省为全体参与者人数",
    )
    deadline: datetime = Field(
        ...,
        description="截止时间，ISO-8601；不带时区偏移时按 UTC 处理",
    )
    creator_id: str | None = Field(default=None, description="创建者标识，可选")
    participant_groups: dict[str, str] | None = Field(
        default=None,
        description=(
            "可选，分组配额策略：参与者标识 -> 组名。每个参与者必须恰好属于一组；"
            "须与 group_thresholds 同时提供"
        ),
    )
    group_thresholds: dict[str, int] | None = Field(
        default=None,
        description=(
            "可选，分组配额策略：组名 -> 该组达标所需报到数（正整数，且不超过组内人数）；"
            "须与 participant_groups 同时提供"
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> "CreateBarrierRequest":
        # 名单去重并保持顺序；空标识无意义
        cleaned: list[str] = []
        for p in self.participants:
            name = p.strip()
            if not name:
                raise ValueError("participants 不得包含空字符串")
            if name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise ValueError("participants 至少需要一个成员")
        self.participants = cleaned

        if self.threshold is not None and self.threshold > len(cleaned):
            raise ValueError("threshold 不能大于参与者人数")

        self._validate_groups(cleaned)
        return self

    def _validate_groups(self, participants: list[str]) -> None:
        """分组配额策略校验（两个字段同时缺省 = 不启用，保持原有行为）。

        启用时要求：每个参与者恰好属于一组；配额为正整数、不超过组内人数；
        各组配额之和不超过总达标人数（否则总 threshold 与分组配额矛盾）。
        """
        groups, quotas = self.participant_groups, self.group_thresholds
        if groups is None and quotas is None:
            return
        if groups is None or quotas is None:
            raise ValueError("participant_groups 与 group_thresholds 必须同时提供")

        # 标识与组名统一去空白；空组名无意义
        stripped: dict[str, str] = {}
        for pid, group in groups.items():
            name = pid.strip()
            gname = group.strip()
            if not gname:
                raise ValueError("participant_groups 的组名不得为空字符串")
            stripped[name] = gname
        self.participant_groups = stripped

        roster = set(participants)
        unknown = sorted(set(stripped) - roster)
        if unknown:
            raise ValueError(f"participant_groups 包含名单外成员: {unknown}")
        missing = sorted(roster - set(stripped))
        if missing:
            raise ValueError(f"存在未分组的参与者（每个参与者必须恰属一组）: {missing}")

        sizes: dict[str, int] = {}
        for gname in stripped.values():
            sizes[gname] = sizes.get(gname, 0) + 1

        for gname, quota in quotas.items():
            if quota <= 0:
                raise ValueError(f"组 {gname!r} 的配额必须是正整数")
            if gname not in sizes:
                raise ValueError(f"组 {gname!r} 没有任何成员，无法设置配额")
            if quota > sizes[gname]:
                raise ValueError(
                    f"组 {gname!r} 的配额 {quota} 超过组内人数 {sizes[gname]}"
                )

        effective_threshold = (
            self.threshold if self.threshold is not None else len(participants)
        )
        total_quota = sum(quotas.values())
        if total_quota > effective_threshold:
            raise ValueError(
                f"各组配额之和 {total_quota} 大于总达标人数 {effective_threshold}，"
                "总配额矛盾"
            )

    def deadline_ts(self) -> float:
        d = self.deadline
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).timestamp()


class GroupProgress(BaseModel):
    """单个分组在分组配额策略下的报到进度。"""

    size: int = Field(description="组内成员人数")
    quota: int = Field(description="该组达标所需报到数（配额）")
    checked: int = Field(description="当前已报到人数（去重后，含达标后的迟到报到）")
    met: bool = Field(description="该组报到数是否已达配额")


class BarrierView(BaseModel):
    barrier_id: str
    status: str = Field(description="waiting | reached | expired | cancelled")
    participants: list[str]
    threshold: int
    deadline: datetime
    created_at: datetime
    checked_in: list[str]
    # waiting 阶段的当前报到人数；其余状态为快照人数
    checked_count: int
    # 达标后冻结的成员名单（仅 reached）
    quorum: list[str] | None = None
    frozen_at: datetime | None = None
    # 截止时仍未报到的成员（仅 expired）
    missing: list[str] | None = None
    # 达标冻结之后才报到的成员（reached 状态的补充信息，不改写结果）
    late: list[str] | None = None
    # 主动取消的原因与时间（仅 cancelled）
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    creator_id: str | None = None
    # 分组配额进度（仅创建时启用分组配额策略的屏障；按组名字典序排列）
    group_progress: dict[str, GroupProgress] | None = None


class CheckinRequest(BaseModel):
    participant_id: str = Field(..., min_length=1, description="参与者标识")


class CheckinResponse(BaseModel):
    barrier_id: str
    status: str = Field(
        description=(
            "checked_in（已报到，继续等待）| already_checked_in（幂等重复报到）"
            "| reached（本次报到触发达标）| late（达标后报到，快照未变）"
        )
    )
    triggered: bool
    quorum: list[str] | None = None
    frozen_at: datetime | None = None
    # 分组配额进度（仅启用分组配额策略的屏障；与 view.group_progress 一致）
    group_progress: dict[str, GroupProgress] | None = None
    view: BarrierView


class CancelRequest(BaseModel):
    reason: str = Field(
        ...,
        min_length=1,
        description="取消原因，非空；去除首尾空白后不得为空字符串",
    )

    @model_validator(mode="after")
    def _validate(self) -> "CancelRequest":
        reason = self.reason.strip()
        if not reason:
            raise ValueError("reason 不得为空白字符串")
        self.reason = reason
        return self


class CancelResponse(BaseModel):
    barrier_id: str
    status: str = Field(
        description="cancelled（本次完成取消）| already_cancelled（重复取消，幂等返回同一结果）"
    )
    reason: str = Field(description="取消原因；重复取消时返回首次保存的原因")
    cancelled_at: datetime
    view: BarrierView


class CheckinEvent(BaseModel):
    seq: int = Field(description="事件序号，从 1 开始单调递增，只追加不回改")
    participant_id: str = Field(description="报到的参与者标识")
    result: str = Field(
        description="checked_in | already_checked_in | reached | late"
    )
    ts: datetime = Field(description="事件发生时间（UTC）")


class BarrierEventsPage(BaseModel):
    barrier_id: str
    events: list[CheckinEvent]
    next_cursor: int = Field(
        description="下一页游标（本页最大 seq）；以其为 cursor 继续查询，无新事件时与入参 cursor 相同"
    )
