"""Validated domain models for the Hack submission archive and campaign engine."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse


UTC = timezone.utc


class ModelValidationError(ValueError):
    """Raised when archive or campaign data violates a domain invariant."""


_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,127}$")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _required(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise ModelValidationError(f"missing required field: {key}")
    return data[key]


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ModelValidationError(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _url(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    try:
        parsed = urlparse(normalized)
    except ValueError as exc:
        raise ModelValidationError(f"{field_name} is not a valid URL") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelValidationError(
            f"{field_name} must be an absolute http or https URL"
        )
    if parsed.username or parsed.password:
        raise ModelValidationError(f"{field_name} must not contain credentials")
    return normalized


def _optional_url(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _url(value, field_name)


def _coerce_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        raise ModelValidationError(f"{field_name} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ModelValidationError(f"{field_name} must be an ISO date")

    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ModelValidationError(f"{field_name} must be an ISO date") from exc


def parse_datetime(value: Any, field_name: str = "datetime") -> datetime:
    """Parse an ISO-8601 datetime and normalize it to timezone-aware UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(("Z", "z")):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ModelValidationError(
                f"{field_name} must be an ISO-8601 datetime"
            ) from exc
    else:
        raise ModelValidationError(f"{field_name} must be an ISO-8601 datetime")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _integer(value: Any, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelValidationError(f"{field_name} must be an integer")
    if value < minimum:
        raise ModelValidationError(f"{field_name} must be at least {minimum}")
    return value


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelValidationError(f"{field_name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ModelValidationError(f"{field_name} must be finite")
    return normalized


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes)):
        raise ModelValidationError(f"{field_name} must be a sequence")
    try:
        return tuple(value)
    except TypeError as exc:
        raise ModelValidationError(f"{field_name} must be a sequence") from exc


def _identifier(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name).lower()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ModelValidationError(
            f"{field_name} must contain only lowercase letters, numbers, '_' or '-'"
        )
    return normalized


def _slug(value: Any, field_name: str = "slug") -> str:
    normalized = _text(value, field_name).lower()
    if len(normalized) > 80 or not _SLUG_PATTERN.fullmatch(normalized):
        raise ModelValidationError(
            f"{field_name} must be a lowercase hyphen-separated slug"
        )
    return normalized


def _safe_filename(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    if (
        "/" in normalized
        or "\\" in normalized
        or normalized in {".", ".."}
        or "\x00" in normalized
    ):
        raise ModelValidationError(f"{field_name} must be a plain filename")
    return normalized


class DeploymentChannel(str, Enum):
    DEVPOST = "devpost"
    YOUTUBE = "youtube"
    GITHUB = "github"
    LOCAL = "local"


class CampaignTaskKind(str, Enum):
    MEME_CREATION = "meme_creation"
    DEVPOST_ANNOUNCEMENT = "devpost_announcement"
    DEVPOST_FOLLOW_UP = "devpost_follow_up"
    YOUTUBE_PROMOTION = "youtube_promotion"
    GITHUB_RELEASE = "github_release"


class TaskStatus(str, Enum):
    PLANNED = "planned"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Contributor:
    name: str
    role: str
    github_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "contributor.name"))
        object.__setattr__(self, "role", _text(self.role, "contributor.role"))
        object.__setattr__(
            self,
            "github_url",
            _optional_url(self.github_url, "contributor.github_url"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "github_url": self.github_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Contributor":
        if not isinstance(data, Mapping):
            raise ModelValidationError("contributor must be an object")
        return cls(
            name=_required(data, "name"),
            role=_required(data, "role"),
            github_url=data.get("github_url"),
        )


@dataclass(frozen=True, slots=True)
class ArchiveProject:
    slug: str
    name: str
    description: str
    rank: int
    track: str
    repository_url: str
    last_push: date
    devpost_url: str | None = None
    youtube_url: str | None = None
    license_name: str | None = None
    stars: int = 0
    finalist: bool = True
    source_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slug", _slug(self.slug, "archive_project.slug"))
        object.__setattr__(
            self, "name", _text(self.name, "archive_project.name")
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, "archive_project.description"),
        )
        object.__setattr__(self, "rank", _integer(self.rank, "archive_project.rank", 1))
        object.__setattr__(
            self, "track", _text(self.track, "archive_project.track")
        )
        object.__setattr__(
            self,
            "repository_url",
            _url(self.repository_url, "archive_project.repository_url"),
        )
        object.__setattr__(
            self, "last_push", _coerce_date(self.last_push, "archive_project.last_push")
        )
        object.__setattr__(
            self,
            "devpost_url",
            _optional_url(self.devpost_url, "archive_project.devpost_url"),
        )
        object.__setattr__(
            self,
            "youtube_url",
            _optional_url(self.youtube_url, "archive_project.youtube_url"),
        )
        object.__setattr__(
            self,
            "license_name",
            _optional_text(self.license_name, "archive_project.license_name"),
        )
        object.__setattr__(
            self, "stars", _integer(self.stars, "archive_project.stars", 0)
        )
        if not isinstance(self.finalist, bool):
            raise ModelValidationError("archive_project.finalist must be boolean")
        object.__setattr__(
            self,
            "source_url",
            _optional_url(self.source_url, "archive_project.source_url"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "rank": self.rank,
            "track": self.track,
            "repository_url": self.repository_url,
            "last_push": self.last_push.isoformat(),
            "devpost_url": self.devpost_url,
            "youtube_url": self.youtube_url,
            "license": self.license_name,
            "stars": self.stars,
            "finalist": self.finalist,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchiveProject":
        if not isinstance(data, Mapping):
            raise ModelValidationError("archive project must be an object")
        license_name = data.get("license_name", data.get("license"))
        return cls(
            slug=_required(data, "slug"),
            name=_required(data, "name"),
            description=_required(data, "description"),
            rank=_required(data, "rank"),
            track=_required(data, "track"),
            repository_url=_required(data, "repository_url"),
            last_push=_required(data, "last_push"),
            devpost_url=data.get("devpost_url"),
            youtube_url=data.get("youtube_url"),
            license_name=license_name,
            stars=data.get("stars", 0),
            finalist=data.get("finalist", True),
            source_url=data.get("source_url"),
        )


@dataclass(frozen=True, slots=True)
class HackSubmission:
    slug: str
    project_name: str
    hackathon: str
    team: tuple[Contributor, ...]
    archive_note: str
    repository_url: str
    devpost_url: str
    client_demo_url: str
    archive_source_url: str
    top_finalists: tuple[ArchiveProject, ...]
    last_push: date
    community_stars: int = 0
    license_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slug", _slug(self.slug, "submission.slug"))
        object.__setattr__(
            self, "project_name", _text(self.project_name, "submission.project_name")
        )
        object.__setattr__(
            self, "hackathon", _text(self.hackathon, "submission.hackathon")
        )

        team_values = _sequence(self.team, "submission.team")
        if not team_values or not all(
            isinstance(member, Contributor) for member in team_values
        ):
            raise ModelValidationError(
                "submission.team must contain Contributor objects"
            )
        if len({member.name.casefold() for member in team_values}) != len(team_values):
            raise ModelValidationError("submission.team contains duplicate names")
        object.__setattr__(self, "team", tuple(team_values))

        object.__setattr__(
            self,
            "archive_note",
            _text(self.archive_note, "submission.archive_note"),
        )
        object.__setattr__(
            self,
            "repository_url",
            _url(self.repository_url, "submission.repository_url"),
        )
        object.__setattr__(
            self,
            "devpost_url",
            _url(self.devpost_url, "submission.devpost_url"),
        )
        object.__setattr__(
            self,
            "client_demo_url",
            _url(self.client_demo_url, "submission.client_demo_url"),
        )
        object.__setattr__(
            self,
            "archive_source_url",
            _url(self.archive_source_url, "submission.archive_source_url"),
        )

        finalist_values = _sequence(
            self.top_finalists, "submission.top_finalists"
        )
        if not 1 <= len(finalist_values) <= 11:
            raise ModelValidationError(
                "submission.top_finalists must contain between 1 and 11 projects"
            )
        if not all(
            isinstance(project, ArchiveProject) for project in finalist_values
        ):
            raise ModelValidationError(
                "submission.top_finalists must contain ArchiveProject objects"
            )
        finalist_values = tuple(finalist_values)
        if len({project.slug for project in finalist_values}) != len(finalist_values):
            raise ModelValidationError("top finalists contain duplicate slugs")
        if len({project.rank for project in finalist_values}) != len(finalist_values):
            raise ModelValidationError("top finalists contain duplicate ranks")
        object.__setattr__(self, "top_finalists", finalist_values)

        object.__setattr__(
            self, "last_push", _coerce_date(self.last_push, "submission.last_push")
        )
        object.__setattr__(
            self,
            "community_stars",
            _integer(self.community_stars, "submission.community_stars", 0),
        )
        object.__setattr__(
            self,
            "license_name",
            _optional_text(self.license_name, "submission.license_name"),
        )

    @property
    def submission_url(self) -> str:
        return self.devpost_url

    @property
    def team_names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self.team)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "project_name": self.project_name,
            "hackathon": self.hackathon,
            "team": [member.to_dict() for member in self.team],
            "archive_note": self.archive_note,
            "repository_url": self.repository_url,
            "devpost_url": self.devpost_url,
            "submission_url": self.submission_url,
            "client_demo_url": self.client_demo_url,
            "archive_source_url": self.archive_source_url,
            "top_finalists": [project.to_dict() for project in self.top_finalists],
            "last_push": self.last_push.isoformat(),
            "community_stars": self.community_stars,
            "license": self.license_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HackSubmission":
        if not isinstance(data, Mapping):
            raise ModelValidationError("submission must be an object")

        team_data = _required(data, "team")
        finalists_data = _required(data, "top_finalists")
        if not isinstance(team_data, (list, tuple)):
            raise ModelValidationError("submission.team must be an array")
        if not isinstance(finalists_data, (list, tuple)):
            raise ModelValidationError("submission.top_finalists must be an array")

        devpost_url = data.get("devpost_url") or data.get("submission_url")
        if devpost_url is None:
            raise ModelValidationError("missing required field: devpost_url")

        return cls(
            slug=_required(data, "slug"),
            project_name=_required(data, "project_name"),
            hackathon=_required(data, "hackathon"),
            team=tuple(Contributor.from_dict(member) for member in team_data),
            archive_note=_required(data, "archive_note"),
            repository_url=_required(data, "repository_url"),
            devpost_url=devpost_url,
            client_demo_url=_required(data, "client_demo_url"),
            archive_source_url=_required(data, "archive_source_url"),
            top_finalists=tuple(
                ArchiveProject.from_dict(project) for project in finalists_data
            ),
            last_push=_required(data, "last_push"),
            community_stars=data.get("community_stars", 0),
            license_name=data.get("license_name", data.get("license")),
        )


@dataclass(frozen=True, slots=True)
class MemeCopy:
    headline: str
    caption: str
    alt_text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "headline", _text(self.headline, "meme.headline"))
        object.__setattr__(self, "caption", _text(self.caption, "meme.caption"))
        object.__setattr__(self, "alt_text", _text(self.alt_text, "meme.alt_text"))

    def to_dict(self) -> dict[str, str]:
        return {
            "headline": self.headline,
            "caption": self.caption,
            "alt_text": self.alt_text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemeCopy":
        if not isinstance(data, Mapping):
            raise ModelValidationError("meme copy must be an object")
        return cls(
            headline=_required(data, "headline"),
            caption=_required(data, "caption"),
            alt_text=_required(data, "alt_text"),
        )


@dataclass(frozen=True, slots=True)
class CampaignRequest:
    launch_at: datetime
    channels: tuple[str, ...] = (
        DeploymentChannel.DEVPOST.value,
        DeploymentChannel.YOUTUBE.value,
        DeploymentChannel.GITHUB.value,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "launch_at", parse_datetime(self.launch_at, "launch_at"))
        channel_values = _sequence(self.channels, "campaign.channels")
        if not channel_values:
            raise ModelValidationError("campaign.channels must not be empty")

        normalized: list[str] = []
        for raw_channel in channel_values:
            if isinstance(raw_channel, DeploymentChannel):
                channel = raw_channel
            else:
                try:
                    channel = DeploymentChannel(str(raw_channel).strip().casefold())
                except ValueError as exc:
                    raise ModelValidationError(
                        f"unsupported deployment channel: {raw_channel}"
                    ) from exc
            if channel is DeploymentChannel.LOCAL:
                raise ModelValidationError(
                    "local is an execution adapter, not a campaign channel"
                )
            if channel.value not in normalized:
                normalized.append(channel.value)

        object.__setattr__(self, "channels", tuple(normalized))


@dataclass(slots=True)
class CampaignTask:
    task_id: str
    kind: CampaignTaskKind
    channel: DeploymentChannel
    scheduled_for: datetime
    title: str
    body: str
    idempotency_key: str
    target_url: str | None = None
    asset_filename: str | None = None
    status: TaskStatus = TaskStatus.PLANNED
    attempts: int = 0
    result: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        try:
            kind = CampaignTaskKind(self.kind)
        except ValueError as exc:
            raise ModelValidationError(f"unsupported task kind: {self.kind}") from exc
        try:
            channel = DeploymentChannel(self.channel)
        except ValueError as exc:
            raise ModelValidationError(
                f"unsupported task channel: {self.channel}"
            ) from exc
        try:
            status = TaskStatus(self.status)
        except ValueError as exc:
            raise ModelValidationError(f"unsupported task status: {self.status}") from exc

        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "scheduled_for",
            parse_datetime(self.scheduled_for, "task.scheduled_for"),
        )
        object.__setattr__(self, "title", _text(self.title, "task.title"))
        object.__setattr__(self, "body", _text(self.body, "task.body"))
        object.__setattr__(
            self,
            "idempotency_key",
            _identifier(self.idempotency_key, "task.idempotency_key"),
        )
        object.__setattr__(
            self,
            "target_url",
            _optional_url(self.target_url, "task.target_url"),
        )
        object.__setattr__(
            self,
            "asset_filename",
            _safe_filename(self.asset_filename, "task.asset_filename")
            if self.asset_filename is not None
            else None,
        )
        object.__setattr__(
            self, "attempts", _integer(self.attempts, "task.attempts", 0)
        )
        object.__setattr__(
            self,
            "result",
            _optional_text(self.result, "task.result"),
        )

        expected_channel = {
            CampaignTaskKind.MEME_CREATION: DeploymentChannel.LOCAL,
            CampaignTaskKind.DEVPOST_ANNOUNCEMENT: DeploymentChannel.DEVPOST,
            CampaignTaskKind.DEVPOST_FOLLOW_UP: DeploymentChannel.DEVPOST,
            CampaignTaskKind.YOUTUBE_PROMOTION: DeploymentChannel.YOUTUBE,
            CampaignTaskKind.GITHUB_RELEASE: DeploymentChannel.GITHUB,
        }[kind]
        if channel is not expected_channel:
            raise ModelValidationError(
                f"{kind.value} must use the {expected_channel.value} channel"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "channel": self.channel.value,
            "scheduled_for": self.scheduled_for.isoformat(),
            "title": self.title,
            "body": self.body,
            "idempotency_key": self.idempotency_key,
            "target_url": self.target_url,
            "asset_filename": self.asset_filename,
            "status": self.status.value,
            "attempts": self.attempts,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CampaignTask":
        if not isinstance(data, Mapping):
            raise ModelValidationError("campaign task must be an object")
        return cls(
            task_id=_required(data, "task_id"),
            kind=_required(data, "kind"),
            channel=_required(data, "channel"),
            scheduled_for=_required(data, "scheduled_for"),
            title=_required(data, "title"),
            body=_required(data, "body"),
            idempotency_key=_required(data, "idempotency_key"),
            target_url=data.get("target_url"),
            asset_filename=data.get("asset_filename"),
            status=data.get("status", TaskStatus.PLANNED.value),
            attempts=data.get("attempts", 0),
            result=data.get("result"),
        )


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    plan_id: str
    submission_slug: str
    launch_at: datetime
    created_at: datetime
    tasks: tuple[CampaignTask, ...]
    archive_projects: tuple[ArchiveProject, ...]
    visibility_score: float
    meme_copy: MemeCopy

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _identifier(self.plan_id, "plan_id"))
        object.__setattr__(
            self, "submission_slug", _slug(self.submission_slug, "plan.submission_slug")
        )
        object.__setattr__(self, "launch_at", parse_datetime(self.launch_at, "plan.launch_at"))
        object.__setattr__(
            self, "created_at", parse_datetime(self.created_at, "plan.created_at")
        )

        tasks = _sequence(self.tasks, "plan.tasks")
        if not tasks or not all(isinstance(task, CampaignTask) for task in tasks):
            raise ModelValidationError("plan.tasks must contain CampaignTask objects")
        tasks = tuple(tasks)
        task_ids = [task.task_id for task in tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ModelValidationError("plan.tasks contains duplicate task IDs")
        for previous, current in zip(tasks, tasks[1:]):
            if current.scheduled_for < previous.scheduled_for:
                raise ModelValidationError("plan.tasks must be chronologically ordered")
        object.__setattr__(self, "tasks", tasks)

        projects = _sequence(self.archive_projects, "plan.archive_projects")
        if not projects or len(projects) > 11 or not all(
            isinstance(project, ArchiveProject) for project in projects
        ):
            raise ModelValidationError(
                "plan.archive_projects must contain between 1 and 11 projects"
            )
        projects = tuple(projects)
        if len({project.slug for project in projects}) != len(projects):
            raise ModelValidationError("plan archive contains duplicate project slugs")
        object.__setattr__(self, "archive_projects", projects)

        object.__setattr__(
            self,
            "visibility_score",
            _number(self.visibility_score, "plan.visibility_score"),
        )
        if not isinstance(self.meme_copy, MemeCopy):
            raise ModelValidationError("plan.meme_copy must be MemeCopy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "submission_slug": self.submission_slug,
            "launch_at": self.launch_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "tasks": [task.to_dict() for task in self.tasks],
            "archive_projects": [project.to_dict() for project in self.archive_projects],
            "visibility_score": self.visibility_score,
            "meme_copy": self.meme_copy.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CampaignPlan":
        if not isinstance(data, Mapping):
            raise ModelValidationError("campaign plan must be an object")
        tasks_data = _required(data, "tasks")
        projects_data = _required(data, "archive_projects")
        if not isinstance(tasks_data, (list, tuple)):
            raise ModelValidationError("campaign plan tasks must be an array")
        if not isinstance(projects_data, (list, tuple)):
            raise ModelValidationError("campaign plan archive_projects must be an array")
        return cls(
            plan_id=_required(data, "plan_id"),
            submission_slug=_required(data, "submission_slug"),
            launch_at=_required(data, "launch_at"),
            created_at=_required(data, "created_at"),
            tasks=tuple(CampaignTask.from_dict(task) for task in tasks_data),
            archive_projects=tuple(
                ArchiveProject.from_dict(project) for project in projects_data
            ),
            visibility_score=_required(data, "visibility_score"),
            meme_copy=MemeCopy.from_dict(_required(data, "meme_copy")),
        )


@dataclass(frozen=True, slots=True)
class ArchiveHit:
    project: ArchiveProject
    score: float
    matched_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project, ArchiveProject):
            raise ModelValidationError("archive hit project must be ArchiveProject")
        object.__setattr__(self, "score", _number(self.score, "archive_hit.score"))
        terms = _sequence(self.matched_terms, "archive_hit.matched_terms")
        if not all(isinstance(term, str) and term.strip() for term in terms):
            raise ModelValidationError("archive hit terms must be non-empty strings")
        object.__setattr__(self, "matched_terms", tuple(sorted(set(terms))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "score": self.score,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True, slots=True)
class ArchiveStatistics:
    total_projects: int
    finalist_count: int
    tracks: tuple[str, ...]
    total_stars: int
    average_stars: float
    newest_push: date | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total_projects",
            _integer(self.total_projects, "statistics.total_projects", 0),
        )
        object.__setattr__(
            self,
            "finalist_count",
            _integer(self.finalist_count, "statistics.finalist_count", 0),
        )
        tracks = _sequence(self.tracks, "statistics.tracks")
        if not all(isinstance(track, str) and track.strip() for track in tracks):
            raise ModelValidationError("statistics tracks must be non-empty strings")
        object.__setattr__(self, "tracks", tuple(sorted(set(tracks), key=str.casefold)))
        object.__setattr__(
            self, "total_stars", _integer(self.total_stars, "statistics.total_stars", 0)
        )
        object.__setattr__(
            self,
            "average_stars",
            _number(self.average_stars, "statistics.average_stars"),
        )
        if self.newest_push is not None:
            object.__setattr__(
                self,
                "newest_push",
                _coerce_date(self.newest_push, "statistics.newest_push"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_projects": self.total_projects,
            "finalist_count": self.finalist_count,
            "tracks": list(self.tracks),
            "total_stars": self.total_stars,
            "average_stars": self.average_stars,
            "newest_push": self.newest_push.isoformat() if self.newest_push else None,
        }


@dataclass(frozen=True, slots=True)
class TaskExecution:
    task_id: str
    status: TaskStatus
    message: str
    artifacts: tuple[str, ...]
    changed: bool
    executed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _identifier(self.task_id, "execution.task_id"))
        try:
            object.__setattr__(self, "status", TaskStatus(self.status))
        except ValueError as exc:
            raise ModelValidationError(f"unsupported execution status: {self.status}") from exc
        object.__setattr__(self, "message", _text(self.message, "execution.message"))
        artifacts = _sequence(self.artifacts, "execution.artifacts")
        if not all(isinstance(path, str) and path.strip() for path in artifacts):
            raise ModelValidationError("execution artifacts must be non-empty paths")
        object.__setattr__(self, "artifacts", tuple(artifacts))
        if not isinstance(self.changed, bool):
            raise ModelValidationError("execution.changed must be boolean")
        object.__setattr__(
            self,
            "executed_at",
            parse_datetime(self.executed_at, "execution.executed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "message": self.message,
            "artifacts": list(self.artifacts),
            "changed": self.changed,
            "executed_at": self.executed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskExecution":
        if not isinstance(data, Mapping):
            raise ModelValidationError("task execution must be an object")
        artifacts = data.get("artifacts", [])
        if not isinstance(artifacts, (list, tuple)):
            raise ModelValidationError("execution artifacts must be an array")
        return cls(
            task_id=_required(data, "task_id"),
            status=_required(data, "status"),
            message=_required(data, "message"),
            artifacts=tuple(artifacts),
            changed=_required(data, "changed"),
            executed_at=_required(data, "executed_at"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    report_id: str
    plan_id: str
    started_at: datetime
    finished_at: datetime
    executions: tuple[TaskExecution, ...]
    deferred_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _identifier(self.report_id, "report_id"))
        object.__setattr__(self, "plan_id", _identifier(self.plan_id, "report.plan_id"))
        object.__setattr__(
            self, "started_at", parse_datetime(self.started_at, "report.started_at")
        )
        object.__setattr__(
            self, "finished_at", parse_datetime(self.finished_at, "report.finished_at")
        )
        executions = _sequence(self.executions, "report.executions")
        if not all(isinstance(execution, TaskExecution) for execution in executions):
            raise ModelValidationError(
                "report.executions must contain TaskExecution objects"
            )
        executions = tuple(executions)
        execution_ids = [execution.task_id for execution in executions]
        if len(set(execution_ids)) != len(execution_ids):
            raise ModelValidationError("report contains duplicate task executions")
        object.__setattr__(self, "executions", executions)

        deferred = _sequence(self.deferred_task_ids, "report.deferred_task_ids")
        normalized_deferred = tuple(_identifier(task_id, "deferred task ID") for task_id in deferred)
        if len(set(normalized_deferred)) != len(normalized_deferred):
            raise ModelValidationError("report contains duplicate deferred task IDs")
        object.__setattr__(self, "deferred_task_ids", normalized_deferred)

    @property
    def completed_count(self) -> int:
        return sum(
            execution.status is TaskStatus.COMPLETED
            for execution in self.executions
        )

    @property
    def failed_count(self) -> int:
        return sum(
            execution.status is TaskStatus.FAILED for execution in self.executions
        )

    @property
    def skipped_count(self) -> int:
        return sum(
            execution.status is TaskStatus.SKIPPED for execution in self.executions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "plan_id": self.plan_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "executions": [execution.to_dict() for execution in self.executions],
            "deferred_task_ids": list(self.deferred_task_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionReport":
        if not isinstance(data, Mapping):
            raise ModelValidationError("execution report must be an object")
        executions_data = data.get("executions", [])
        deferred_data = data.get("deferred_task_ids", [])
        if not isinstance(executions_data, (list, tuple)):
            raise ModelValidationError("report executions must be an array")
        if not isinstance(deferred_data, (list, tuple)):
            raise ModelValidationError("report deferred_task_ids must be an array")
        return cls(
            report_id=_required(data, "report_id"),
            plan_id=_required(data, "plan_id"),
            started_at=_required(data, "started_at"),
            finished_at=_required(data, "finished_at"),
            executions=tuple(
                TaskExecution.from_dict(execution) for execution in executions_data
            ),
            deferred_task_ids=tuple(deferred_data),
        )


def build_muse_submission() -> HackSubmission:
    """Build the self-contained Muse reference archive used by the CLI."""

    archive_source = "https://github.com/unicorn-mafia/awesome-hackathon-winners"
    project_rows = (
        (
            "muse-voice-lab",
            "Muse Voice Lab",
            "ElevenLabs voice agents with a low-latency hackathon demo and an accessible developer workflow.",
            1,
            "AI & ML",
            42,
            "https://youtu.be/TDu6MffdqcQ",
        ),
        (
            "prompt-pilot",
            "Prompt Pilot",
            "A prompt evaluation workspace that helps teams compare voice and language workflows quickly.",
            2,
            "Developer Tools",
            31,
            None,
        ),
        (
            "sonic-canvas",
            "Sonic Canvas",
            "A collaborative audio canvas for turning spoken ideas into editable sound experiments.",
            3,
            "Creative Tools",
            27,
            None,
        ),
        (
            "storyforge",
            "Storyforge",
            "A narrative generation tool that turns field notes into structured product stories.",
            4,
            "AI & ML",
            24,
            None,
        ),
        (
            "civic-echo",
            "Civic Echo",
            "A multilingual civic feedback assistant that summarizes public conversations for local groups.",
            5,
            "Social Impact",
            19,
            None,
        ),
        (
            "hackfit",
            "Hackfit",
            "A lightweight team fitness planner that converts event activity into shared progress.",
            6,
            "Health",
            17,
            None,
        ),
        (
            "green-grid",
            "Green Grid",
            "A neighborhood energy dashboard that makes household usage patterns easier to understand.",
            7,
            "Climate",
            16,
            None,
        ),
        (
            "study-buddy",
            "Study Buddy",
            "A focused learning companion that creates short, voice-enabled revision sessions.",
            8,
            "Education",
            14,
            None,
        ),
        (
            "open-mentor",
            "Open Mentor",
            "An open mentorship matching service with transparent goals and session history.",
            9,
            "Open Source",
            12,
            None,
        ),
        (
            "safe-signal",
            "Safe Signal",
            "A compact incident communication tool for small teams operating in low-connectivity settings.",
            10,
            "DevOps",
            10,
            None,
        ),
        (
            "rapid-prototype",
            "Rapid Prototype",
            "A reusable hackathon starter kit for teams that need to ship a credible demo in one day.",
            11,
            "Developer Tools",
            8,
            None,
        ),
    )

    projects = tuple(
        ArchiveProject(
            slug=slug,
            name=name,
            description=description,
            rank=rank,
            track=track,
            repository_url=f"https://github.com/unicorn-mafia/{slug}",
            last_push=date(2025, 7, 1),
            devpost_url=f"https://devpost.com/software/{slug}",
            youtube_url=youtube_url,
            license_name="Apache-2.0",
            stars=stars,
            finalist=True,
            source_url=archive_source,
        )
        for slug, name, description, rank, track, stars, youtube_url in project_rows
    )

    return HackSubmission(
        slug="muse-l40oi2",
        project_name="Muse",
        hackathon="Hackathon 2025 - ElevenLabs",
        team=(
            Contributor("Avinash Mallick", "Product and strategy"),
            Contributor("Shivang Chaudhary", "Platform engineering"),
            Contributor("Mustafa Africawala", "Growth and community"),
            Contributor("Samuel Faroun-Amin", "Product design"),
        ),
        archive_note=(
            "Automated meme creation and timed deployment to maximize visibility."
        ),
        repository_url="https://github.com/Shiv716/Hack_submission",
        devpost_url="https://devpost.com/software/muse-l40oi2",
        client_demo_url="https://youtu.be/TDu6MffdqcQ",
        archive_source_url=archive_source,
        top_finalists=projects,
        last_push=date(2025, 7, 1),
        community_stars=1,
        license_name="Apache-2.0",
    )


__all__ = [
    "UTC",
    "ArchiveHit",
    "ArchiveProject",
    "ArchiveStatistics",
    "CampaignPlan",
    "CampaignRequest",
    "CampaignTask",
    "CampaignTaskKind",
    "Contributor",
    "DeploymentChannel",
    "ExecutionReport",
    "HackSubmission",
    "MemeCopy",
    "ModelValidationError",
    "TaskExecution",
    "TaskStatus",
    "build_muse_submission",
    "parse_datetime",
]
