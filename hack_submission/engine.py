"""Business logic for archive indexing, campaign planning, scheduling, and execution."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    UTC,
    ArchiveHit,
    ArchiveProject,
    ArchiveStatistics,
    CampaignPlan,
    CampaignRequest,
    CampaignTask,
    CampaignTaskKind,
    DeploymentChannel,
    ExecutionReport,
    HackSubmission,
    MemeCopy,
    ModelValidationError,
    TaskExecution,
    TaskStatus,
    build_muse_submission,
    parse_datetime,
)


class EngineError(RuntimeError):
    """Base exception for campaign engine failures."""


class StateStoreError(EngineError):
    """Raised when campaign state cannot be read or written."""


class PlanNotFoundError(EngineError):
    """Raised when a requested plan is absent from the state store."""


class ArchiveIndex:
    """An immutable, ranked index over curated finalist records."""

    def __init__(self, projects: Iterable[ArchiveProject]) -> None:
        if isinstance(projects, (str, bytes)):
            raise EngineError("archive projects must be an iterable of ArchiveProject")
        try:
            records = tuple(projects)
        except TypeError as exc:
            raise EngineError("archive projects must be iterable") from exc

        if not records:
            raise EngineError("archive index requires at least one project")
        if not all(isinstance(project, ArchiveProject) for project in records):
            raise EngineError("archive index contains a non-ArchiveProject value")

        slugs = [project.slug for project in records]
        if len(set(slugs)) != len(slugs):
            raise EngineError("archive index contains duplicate project slugs")

        self.projects = tuple(
            sorted(
                records,
                key=lambda project: (
                    project.rank,
                    project.name.casefold(),
                    project.slug,
                ),
            )
        )

    def __len__(self) -> int:
        return len(self.projects)

    def __iter__(self):
        return iter(self.projects)

    @classmethod
    def from_submission(cls, submission: HackSubmission) -> "ArchiveIndex":
        if not isinstance(submission, HackSubmission):
            raise EngineError("submission must be a HackSubmission")
        return cls(submission.top_finalists)

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any] | ArchiveProject]) -> "ArchiveIndex":
        if isinstance(records, (str, bytes)):
            raise EngineError("archive records must be iterable")
        try:
            iterator = iter(records)
        except TypeError as exc:
            raise EngineError("archive records must be iterable") from exc

        projects: list[ArchiveProject] = []
        for record in iterator:
            if isinstance(record, ArchiveProject):
                projects.append(record)
            elif isinstance(record, Mapping):
                projects.append(ArchiveProject.from_dict(record))
            else:
                raise EngineError("archive record must be an object or ArchiveProject")
        return cls(projects)

    def top(self, count: int) -> tuple[ArchiveProject, ...]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise EngineError("top count must be a positive integer")
        return self.projects[:count]

    def search(
        self,
        query: str,
        track: str | None = None,
        limit: int = 10,
    ) -> tuple[ArchiveHit, ...]:
        """Return weighted matches across names, descriptions, tracks, and slugs.

        Exact phrase matches receive the strongest signal. Individual tokens are
        weighted by the field in which they occur, then rank and traction provide
        deterministic tie-breaking signals.
        """

        if not isinstance(query, str):
            raise EngineError("archive search query must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise EngineError("archive search limit must be between 1 and 1000")

        phrase = query.strip().casefold()
        tokens = tuple(
            dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9+#.-]*", phrase))
        )
        track_filter = track.strip().casefold() if isinstance(track, str) else None
        if track is not None and not isinstance(track, str):
            raise EngineError("archive search track must be a string")

        hits: list[ArchiveHit] = []
        for project in self.projects:
            if track_filter and project.track.casefold() != track_filter:
                continue

            name = project.name.casefold()
            description = project.description.casefold()
            project_track = project.track.casefold()
            slug = project.slug.casefold()
            combined = " ".join((name, description, project_track, slug))
            matched: set[str] = set()
            score = 0.0
            phrase_matched = bool(phrase and phrase in combined)
            if phrase_matched:
                score += 25.0
                matched.add(phrase)

            for token in tokens:
                token_matched = False
                if token in name:
                    score += 8.0
                    matched.add(token)
                    token_matched = True
                elif token in description:
                    score += 4.0
                    matched.add(token)
                    token_matched = True
                if token in project_track:
                    score += 3.0
                    matched.add(token)
                    token_matched = True
                if token in slug:
                    score += 2.0
                    matched.add(token)
                    token_matched = True
                if token in combined and not token_matched:
                    score += 1.0
                    matched.add(token)

            if tokens and not matched:
                continue
            if not tokens and phrase and not phrase_matched:
                continue
            if not tokens and not phrase:
                score = 1.0

            score += min(4.0, math.log1p(project.stars + 1))
            score += max(0.0, 12.0 - project.rank) / 10.0
            hits.append(
                ArchiveHit(
                    project=project,
                    score=round(score, 4),
                    matched_terms=tuple(sorted(matched)),
                )
            )

        hits.sort(
            key=lambda hit: (
                -hit.score,
                hit.project.rank,
                hit.project.name.casefold(),
                hit.project.slug,
            )
        )
        return tuple(hits[:limit])

    def statistics(self) -> ArchiveStatistics:
        tracks = tuple(
            sorted({project.track for project in self.projects}, key=str.casefold)
        )
        total_stars = sum(project.stars for project in self.projects)
        average_stars = round(total_stars / len(self.projects), 2)
        newest_push = max(
            (project.last_push for project in self.projects),
            default=None,
        )
        return ArchiveStatistics(
            total_projects=len(self.projects),
            finalist_count=sum(project.finalist for project in self.projects),
            tracks=tracks,
            total_stars=total_stars,
            average_stars=average_stars,
            newest_push=newest_push,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "projects": [project.to_dict() for project in self.projects],
            "statistics": self.statistics().to_dict(),
        }


def validate_submission(submission: HackSubmission) -> dict[str, Any]:
    """Validate cross-record evidence and return an auditable validation summary."""

    if not isinstance(submission, HackSubmission):
        raise EngineError("submission validation requires a HackSubmission")

    index = ArchiveIndex.from_submission(submission)
    statistics = index.statistics()
    warnings: list[str] = []

    if len(submission.team) < 4:
        warnings.append("fewer than four contributors reduces execution coverage")
    if submission.community_stars == 0:
        warnings.append("no public repository traction is recorded")
    if any(project.rank > 11 for project in submission.top_finalists):
        warnings.append("one or more projects fall outside the top eleven ranks")
    if len(statistics.tracks) < 2:
        warnings.append("the finalist archive covers fewer than two tracks")

    evidence_links = {
        "repository": submission.repository_url,
        "devpost": submission.devpost_url,
        "client_demo": submission.client_demo_url,
        "archive_source": submission.archive_source_url,
    }
    return {
        "valid": True,
        "submission_slug": submission.slug,
        "project_name": submission.project_name,
        "hackathon": submission.hackathon,
        "team": list(submission.team_names),
        "top_finalist_count": len(submission.top_finalists),
        "top_finalist_slugs": [
            project.slug for project in submission.top_finalists
        ],
        "evidence_links": evidence_links,
        "archive_statistics": statistics.to_dict(),
        "warnings": warnings,
    }


class MemeComposer:
    """Compose deterministic, submission-specific meme copy from archive evidence."""

    def compose(
        self,
        submission: HackSubmission,
        featured_project: ArchiveProject,
    ) -> MemeCopy:
        if not isinstance(submission, HackSubmission):
            raise EngineError("meme composition requires a HackSubmission")
        if not isinstance(featured_project, ArchiveProject):
            raise EngineError("meme composition requires an ArchiveProject")

        seed = int(
            hashlib.sha256(
                f"{submission.slug}|{featured_project.slug}|{submission.hackathon}".encode(
                    "utf-8"
                )
            ).hexdigest()[:8],
            16,
        )
        variants = (
            (
                "Hackathon mode: activated",
                f"{featured_project.name} turns a live demo into a story the judges remember.",
            ),
            (
                "One more tab open",
                f"{submission.project_name} meets {submission.hackathon} with a workflow people can try.",
            ),
            (
                "The demo is the pitch",
                f"POV: {featured_project.track} innovation becomes useful before the timer ends.",
            ),
            (
                "Ship the idea",
                f"{submission.project_name} gives hackathon teams a faster path from prompt to prototype.",
            ),
        )
        headline, caption = variants[seed % len(variants)]
        caption = f"{caption} Explore: {submission.devpost_url}"
        if len(caption) > 280:
            caption = caption[:279].rstrip() + "…"

        alt_text = (
            f"Meme for {submission.project_name}: {headline}. "
            f"Featured archive project: {featured_project.name}, "
            f"ranked #{featured_project.rank} in {featured_project.track}. "
            f"Submission: {submission.devpost_url}."
        )
        if len(alt_text) > 300:
            alt_text = alt_text[:299].rstrip() + "…"
        return MemeCopy(headline=headline, caption=caption, alt_text=alt_text)


def calculate_visibility_score(
    submission: HackSubmission,
    archive_index: ArchiveIndex,
    channels: Iterable[str | DeploymentChannel],
) -> float:
    """Calculate a bounded prioritization score for campaign planning.

    This is an internal prioritization heuristic rather than a claim about any
    external platform's ranking algorithm. It rewards evidence, team coverage,
    archive depth, traction, and the number of selected distribution channels.
    """

    if not isinstance(submission, HackSubmission):
        raise EngineError("visibility scoring requires a HackSubmission")
    if not isinstance(archive_index, ArchiveIndex):
        raise EngineError("visibility scoring requires an ArchiveIndex")

    normalized_channels: set[DeploymentChannel] = set()
    for raw_channel in channels:
        if isinstance(raw_channel, DeploymentChannel):
            channel = raw_channel
        else:
            try:
                channel = DeploymentChannel(str(raw_channel).casefold())
            except ValueError as exc:
                raise EngineError(f"unsupported deployment channel: {raw_channel}") from exc
        if channel is DeploymentChannel.LOCAL:
            raise EngineError("local cannot be selected as a visibility channel")
        normalized_channels.add(channel)

    statistics = archive_index.statistics()
    score = 20.0
    score += min(18.0, statistics.finalist_count * 1.6)
    score += min(18.0, len(statistics.tracks) * 3.0)
    score += min(15.0, math.log1p(submission.community_stars + 1) * 4.0)
    score += min(12.0, len(submission.team) * 2.5)
    score += min(15.0, len(normalized_channels) * 5.0)
    score += 5.0 if submission.client_demo_url else 0.0
    score += 4.0 if submission.repository_url else 0.0
    score += 2.0 if submission.archive_note else 0.0
    if archive_index.top(1)[0].rank <= 3:
        score += 2.0
    return round(min(100.0, score), 2)


def _task_identifier(
    submission_slug: str,
    kind: CampaignTaskKind,
    scheduled_for: datetime,
) -> str:
    stamp = scheduled_for.strftime("%Y%m%dT%H%M%SZ")
    kind_token = kind.value.replace("_", "-")
    candidate = f"{submission_slug}-{kind_token}-{stamp}".lower()
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    return f"{submission_slug[:48]}-{kind_token}-{stamp}-{digest}".lower()


class CampaignPlanner:
    """Turn validated submission evidence into chronological visibility tasks."""

    def __init__(
        self,
        archive_index: ArchiveIndex | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if archive_index is not None and not isinstance(archive_index, ArchiveIndex):
            raise EngineError("planner archive_index must be an ArchiveIndex")
        self.archive_index = archive_index
        self.clock = clock or (lambda: datetime.now(UTC))

    def build_plan(
        self,
        submission: HackSubmission,
        request: CampaignRequest,
        *,
        created_at: datetime | None = None,
    ) -> CampaignPlan:
        if not isinstance(submission, HackSubmission):
            raise EngineError("campaign planning requires a HackSubmission")
        if not isinstance(request, CampaignRequest):
            raise EngineError("campaign planning requires a CampaignRequest")

        archive_index = self.archive_index or ArchiveIndex.from_submission(submission)
        known_slugs = {project.slug for project in archive_index}
        missing_slugs = [
            project.slug
            for project in submission.top_finalists
            if project.slug not in known_slugs
        ]
        if missing_slugs:
            raise EngineError(
                "planner archive is missing finalist records: "
                + ", ".join(sorted(missing_slugs))
            )

        launch_at = request.launch_at
        project_snapshot = archive_index.top(min(11, len(archive_index)))
        featured_project = project_snapshot[0]
        meme_copy = MemeComposer().compose(submission, featured_project)
        team_line = ", ".join(submission.team_names)
        archive_line = "; ".join(
            f"#{project.rank} {project.name}" for project in submission.top_finalists[:5]
        )
        base_body = (
            f"{submission.project_name} was built by {team_line} for "
            f"{submission.hackathon}.\n"
            f"Archive evidence: {len(submission.top_finalists)} curated finalist "
            f"records; {submission.community_stars} GitHub stars.\n"
            f"Archive note: {submission.archive_note}\n"
            f"Featured records: {archive_line}"
        )

        task_specs: list[
            tuple[
                CampaignTaskKind,
                DeploymentChannel,
                int,
                str,
                str,
                str | None,
            ]
        ] = [
            (
                CampaignTaskKind.MEME_CREATION,
                DeploymentChannel.LOCAL,
                -48,
                f"Create the {submission.project_name} launch meme",
                f"{meme_copy.caption}\n\nSuggested alt text: {meme_copy.alt_text}",
                None,
            )
        ]
        channels = set(request.channels)
        if DeploymentChannel.DEVPOST.value in channels:
            task_specs.extend(
                [
                    (
                        CampaignTaskKind.DEVPOST_ANNOUNCEMENT,
                        DeploymentChannel.DEVPOST,
                        -24,
                        f"Announce {submission.project_name} on Devpost",
                        f"{base_body}\n\nSubmission: {submission.devpost_url}",
                        submission.devpost_url,
                    ),
                    (
                        CampaignTaskKind.DEVPOST_FOLLOW_UP,
                        DeploymentChannel.DEVPOST,
                        24,
                        f"Share the {submission.project_name} follow-up",
                        (
                            f"{base_body}\n\nThe launch window is active. "
                            f"Revisit the submission and client demo: "
                            f"{submission.devpost_url} | {submission.client_demo_url}"
                        ),
                        submission.devpost_url,
                    ),
                ]
            )
        if DeploymentChannel.GITHUB.value in channels:
            task_specs.append(
                (
                    CampaignTaskKind.GITHUB_RELEASE,
                    DeploymentChannel.GITHUB,
                    -6,
                    f"Publish the {submission.project_name} repository update",
                    (
                        f"{base_body}\n\nRepository: {submission.repository_url}\n"
                        f"License: {submission.license_name or 'not recorded'}"
                    ),
                    submission.repository_url,
                )
            )
        if DeploymentChannel.YOUTUBE.value in channels:
            task_specs.append(
                (
                    CampaignTaskKind.YOUTUBE_PROMOTION,
                    DeploymentChannel.YOUTUBE,
                    -2,
                    f"Promote the {submission.project_name} client demo",
                    (
                        f"{base_body}\n\nWatch the client demo: "
                        f"{submission.client_demo_url}\n"
                        f"Read the submission: {submission.devpost_url}"
                    ),
                    submission.client_demo_url,
                )
            )

        tasks: list[CampaignTask] = []
        for sequence, (
            kind,
            channel,
            offset_hours,
            title,
            body,
            target_url,
        ) in enumerate(task_specs, start=1):
            try:
                scheduled_for = launch_at + timedelta(hours=offset_hours)
            except OverflowError as exc:
                raise EngineError("campaign schedule exceeds datetime range") from exc
            task_id = _task_identifier(submission.slug, kind, scheduled_for)
            idempotency_material = (
                f"{submission.slug}|{kind.value}|{channel.value}|"
                f"{scheduled_for.isoformat()}|{sequence}"
            )
            idempotency_key = hashlib.sha256(
                idempotency_material.encode("utf-8")
            ).hexdigest()
            tasks.append(
                CampaignTask(
                    task_id=task_id,
                    kind=kind,
                    channel=channel,
                    scheduled_for=scheduled_for,
                    title=title,
                    body=body,
                    idempotency_key=idempotency_key,
                    target_url=target_url,
                    asset_filename=(
                        f"{task_id}.svg" if kind is CampaignTaskKind.MEME_CREATION else None
                    ),
                )
            )

        tasks.sort(
            key=lambda task: (
                task.scheduled_for,
                task.kind.value,
                task.channel.value,
                task.task_id,
            )
        )
        plan_material = json.dumps(
            {
                "submission": submission.to_dict(),
                "launch_at": launch_at.isoformat(),
                "channels": list(request.channels),
                "tasks": [task.idempotency_key for task in tasks],
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        plan_id = "plan-" + hashlib.sha256(plan_material.encode("utf-8")).hexdigest()[:20]
        creation_time = (
            parse_datetime(created_at, "created_at")
            if created_at is not None
            else parse_datetime(self.clock(), "created_at")
        )
        return CampaignPlan(
            plan_id=plan_id,
            submission_slug=submission.slug,
            launch_at=launch_at,
            created_at=creation_time,
            tasks=tuple(tasks),
            archive_projects=project_snapshot,
            visibility_score=calculate_visibility_score(
                submission, archive_index, request.channels
            ),
            meme_copy=meme_copy,
        )

    def plan(
        self,
        submission: HackSubmission,
        request: CampaignRequest,
        *,
        created_at: datetime | None = None,
    ) -> CampaignPlan:
        """Compatibility-friendly alias for build_plan."""

        return self.build_plan(submission, request, created_at=created_at)


def create_campaign_plan(
    submission: HackSubmission,
    launch_at: datetime,
    channels: Iterable[str | DeploymentChannel] = (
        DeploymentChannel.DEVPOST.value,
        DeploymentChannel.YOUTUBE.value,
        DeploymentChannel.GITHUB.value,
    ),
    *,
    archive_index: ArchiveIndex | None = None,
    created_at: datetime | None = None,
) -> CampaignPlan:
    """Convenience API for creating a validated campaign plan."""

    request = CampaignRequest(launch_at=launch_at, channels=tuple(channels))
    return CampaignPlanner(archive_index=archive_index).build_plan(
        submission, request, created_at=created_at
    )


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text atomically so interrupted runs cannot leave partial files."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, mode)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class JsonStateStore:
    """Persist plans and execution reports using an atomic versioned JSON file."""

    VERSION = 1

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    @classmethod
    def _validate_state(cls, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise StateStoreError("campaign state must be a JSON object")
        if state.get("version") != cls.VERSION:
            raise StateStoreError(
                f"unsupported campaign state version: {state.get('version')!r}"
            )
        plans = state.get("plans")
        if not isinstance(plans, dict):
            raise StateStoreError("campaign state plans must be an object")
        for plan_id, record in plans.items():
            if not isinstance(plan_id, str) or not plan_id:
                raise StateStoreError("campaign state contains an invalid plan ID")
            if not isinstance(record, dict) or "plan" not in record:
                raise StateStoreError(f"state record for {plan_id} is missing a plan")
            reports = record.get("reports", [])
            if not isinstance(reports, list):
                raise StateStoreError(f"state reports for {plan_id} must be an array")
        return state

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": self.VERSION, "plans": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StateStoreError(
                f"invalid JSON in campaign state {self.path}: {exc.msg}"
            ) from exc
        except OSError as exc:
            raise StateStoreError(f"cannot read campaign state {self.path}: {exc}") from exc
        return self._validate_state(state)

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def save(self, state: Mapping[str, Any]) -> None:
        with self._lock:
            normalized = self._validate_state(dict(state))
            serialized = json.dumps(
                normalized,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            atomic_write_text(self.path, serialized, mode=0o600)

    def _entry_unlocked(
        self,
        state: Mapping[str, Any],
        plan_id: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        record = state["plans"].get(plan_id)
        if record is None and required:
            raise PlanNotFoundError(f"campaign plan not found: {plan_id}")
        if record is not None and not isinstance(record, dict):
            raise StateStoreError(f"state record for {plan_id} is invalid")
        return record

    def save_plan(self, plan: CampaignPlan) -> None:
        if not isinstance(plan, CampaignPlan):
            raise StateStoreError("save_plan requires a CampaignPlan")
        with self._lock:
            state = self._load_unlocked()
            record = self._entry_unlocked(state, plan.plan_id, required=False)
            reports = list(record.get("reports", [])) if record else []
            state["plans"][plan.plan_id] = {
                "plan": plan.to_dict(),
                "reports": reports,
            }
            self.save(state)

    def get_plan(self, plan_id: str) -> CampaignPlan:
        with self._lock:
            state = self._load_unlocked()
            record = self._entry_unlocked(state, plan_id)
            assert record is not None
            try:
                return CampaignPlan.from_dict(record["plan"])
            except ModelValidationError as exc:
                raise StateStoreError(
                    f"stored plan {plan_id} is invalid: {exc}"
                ) from exc

    def get_reports(self, plan_id: str) -> tuple[ExecutionReport, ...]:
        with self._lock:
            state = self._load_unlocked()
            record = self._entry_unlocked(state, plan_id, required=False)
            if record is None:
                return ()
            reports = record.get("reports", [])
            parsed: list[ExecutionReport] = []
            try:
                for report_data in reports:
                    parsed.append(ExecutionReport.from_dict(report_data))
            except ModelValidationError as exc:
                raise StateStoreError(
                    f"stored reports for {plan_id} are invalid: {exc}"
                ) from exc
            return tuple(parsed)

    def record_execution(
        self,
        plan: CampaignPlan,
        report: ExecutionReport,
    ) -> None:
        if not isinstance(plan, CampaignPlan):
            raise StateStoreError("record_execution requires a CampaignPlan")
        if not isinstance(report, ExecutionReport):
            raise StateStoreError("record_execution requires an ExecutionReport")
        if report.plan_id != plan.plan_id:
            raise StateStoreError("execution report does not belong to the plan")

        with self._lock:
            state = self._load_unlocked()
            record = self._entry_unlocked(state, plan.plan_id, required=False)
            reports = list(record.get("reports", [])) if record else []
            if not any(
                isinstance(item, Mapping) and item.get("report_id") == report.report_id
                for item in reports
            ):
                reports.append(report.to_dict())
            state["plans"][plan.plan_id] = {
                "plan": plan.to_dict(),
                "reports": reports,
            }
            self.save(state)

    def status(self, plan_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            if plan_id is None:
                return state
            record = self._entry_unlocked(state, plan_id)
            assert record is not None
            return {
                "plan": record["plan"],
                "reports": record.get("reports", []),
            }


class LocalArtifactDeployer:
    """Write provider-ready deployment payloads and durable Markdown receipts.

    The adapter performs the local half of a deployment deterministically and
    without credentials. A production provider client can consume the same
    payload shape and replace this class without changing campaign semantics.
    """

    def publish(
        self,
        task: CampaignTask,
        plan: CampaignPlan,
        output_dir: Path,
        published_at: datetime,
    ) -> tuple[str, ...]:
        if task.channel is DeploymentChannel.LOCAL:
            raise EngineError("local channel has no deployment adapter")
        deployment_dir = Path(output_dir).expanduser() / "deployments"
        deployment_dir.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {
            "schema_version": 1,
            "delivery_status": "local_receipt",
            "adapter": "durable-local",
            "plan_id": plan.plan_id,
            "task_id": task.task_id,
            "kind": task.kind.value,
            "provider": task.channel.value,
            "title": task.title,
            "body": task.body,
            "target_url": task.target_url,
            "scheduled_for": task.scheduled_for.isoformat(),
            "published_at": published_at.isoformat(),
            "idempotency_key": task.idempotency_key,
        }
        if task.channel is DeploymentChannel.DEVPOST:
            payload["software_url"] = task.target_url
        elif task.channel is DeploymentChannel.YOUTUBE:
            payload["video_url"] = task.target_url
        elif task.channel is DeploymentChannel.GITHUB:
            payload["repository_url"] = task.target_url

        json_path = deployment_dir / f"{task.task_id}.json"
        markdown_path = deployment_dir / f"{task.task_id}.md"
        atomic_write_text(
            json_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o644,
        )
        target_line = (
            f"\nTarget: {task.target_url}\n" if task.target_url else "\n"
        )
        markdown = (
            f"# {task.title}\n\n"
            f"Provider: {task.channel.value}\n"
            f"Scheduled: {task.scheduled_for.isoformat()}\n"
            f"Idempotency key: {task.idempotency_key}\n"
            f"{target_line}\n"
            f"{task.body}\n"
        )
        atomic_write_text(markdown_path, markdown, mode=0o644)
        return (str(json_path.resolve()), str(markdown_path.resolve()))


def _wrap_text(text: str, maximum_characters: int) -> list[str]:
    """Wrap text by words, splitting pathological long words by character."""

    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""

    for word in words:
        if len(word) > maximum_characters:
            if current:
                lines.append(current)
                current = ""
            for start in range(0, len(word), maximum_characters):
                lines.append(word[start : start + maximum_characters])
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= maximum_characters:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_meme(
    meme_copy: MemeCopy,
    task: CampaignTask,
    output_dir: Path,
) -> tuple[str, ...]:
    """Render escaped, deterministic SVG artwork and a plain-text caption."""

    if not isinstance(meme_copy, MemeCopy):
        raise EngineError("meme renderer requires MemeCopy")
    if not isinstance(task, CampaignTask):
        raise EngineError("meme renderer requires CampaignTask")

    asset_dir = Path(output_dir).expanduser() / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_name = task.asset_filename or f"{task.task_id}.svg"
    svg_path = asset_dir / asset_name
    text_path = asset_dir / f"{asset_name}.txt"

    headline_lines = _wrap_text(meme_copy.headline, 24)[:3]
    caption_lines = _wrap_text(meme_copy.caption, 48)[:5]
    digest = hashlib.sha256(task.task_id.encode("utf-8")).hexdigest()
    accent = ("#7c3aed", "#0f766e", "#c2410c", "#1d4ed8")[int(digest[:2], 16) % 4]

    headline_markup = "\n".join(
        f'    <text x="60" y="{155 + index * 68}" class="headline">'
        f"{html.escape(line, quote=True)}</text>"
        for index, line in enumerate(headline_lines)
    )
    caption_markup = "\n".join(
        f'    <text x="60" y="{390 + index * 43}" class="caption">'
        f"{html.escape(line, quote=True)}</text>"
        for index, line in enumerate(caption_lines)
    )
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#111827"/>
  <rect x="40" y="40" width="1120" height="550" rx="24" fill="#1f2937" stroke="{accent}" stroke-width="8"/>
  <circle cx="1050" cy="120" r="54" fill="{accent}" opacity="0.9"/>
  <text x="1005" y="134" text-anchor="middle" fill="#ffffff" font-size="44" font-family="Arial, sans-serif">M</text>
  <g font-family="Arial, sans-serif" fill="#f9fafb">
{headline_markup}
    <text x="60" y="325" fill="{accent}" font-size="24" font-weight="700" letter-spacing="2">HACK SUBMISSION</text>
{caption_markup}
  </g>
  <text x="60" y="555" fill="#9ca3af" font-size="18" font-family="Arial, sans-serif">Generated from validated finalist archive evidence</text>
</svg>
'''
    caption_document = (
        f"Headline: {meme_copy.headline}\n\n"
        f"Caption: {meme_copy.caption}\n\n"
        f"Alt text: {meme_copy.alt_text}\n"
    )
    atomic_write_text(svg_path, svg, mode=0o644)
    atomic_write_text(text_path, caption_document, mode=0o644)
    return (str(svg_path.resolve()), str(text_path.resolve()))


class CampaignExecutor:
    """Execute due campaign tasks with durable, idempotent state transitions."""

    def __init__(
        self,
        meme_renderer: Callable[[MemeCopy, CampaignTask, Path], tuple[str, ...]] | None = None,
        deployer: LocalArtifactDeployer | None = None,
    ) -> None:
        self.meme_renderer = meme_renderer or render_meme
        self.deployer = deployer or LocalArtifactDeployer()

    def execute(
        self,
        plan: CampaignPlan,
        output_dir: Path,
        *,
        now: datetime | None = None,
        allow_early: bool = False,
        completed_task_ids: set[str] | None = None,
    ) -> ExecutionReport:
        if not isinstance(plan, CampaignPlan):
            raise EngineError("campaign execution requires a CampaignPlan")
        execution_time = (
            parse_datetime(now, "now") if now is not None else datetime.now(UTC)
        )
        destination = Path(output_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)

        completed = set(completed_task_ids or ())
        completed.update(
            task.task_id for task in plan.tasks if task.status is TaskStatus.COMPLETED
        )
        deferred: list[str] = []
        executions: list[TaskExecution] = []

        for task in plan.tasks:
            if task.task_id in completed:
                executions.append(
                    TaskExecution(
                        task_id=task.task_id,
                        status=TaskStatus.SKIPPED,
                        message="Already completed; idempotency preserved",
                        artifacts=(),
                        changed=False,
                        executed_at=execution_time,
                    )
                )
                continue

            if execution_time < task.scheduled_for and not allow_early:
                deferred.append(task.task_id)
                executions.append(
                    TaskExecution(
                        task_id=task.task_id,
                        status=TaskStatus.SKIPPED,
                        message=(
                            "Deferred until "
                            f"{task.scheduled_for.isoformat()}"
                        ),
                        artifacts=(),
                        changed=False,
                        executed_at=execution_time,
                    )
                )
                continue

            task.attempts += 1
            try:
                if task.kind is CampaignTaskKind.MEME_CREATION:
                    artifacts = self.meme_renderer(
                        plan.meme_copy, task, destination
                    )
                    message = "Generated escaped SVG meme and caption text"
                else:
                    artifacts = self.deployer.publish(
                        task, plan, destination, execution_time
                    )
                    message = (
                        "Wrote provider-ready deployment receipts for "
                        f"{task.channel.value}"
                    )
                normalized_artifacts = tuple(str(path) for path in artifacts)
                task.status = TaskStatus.COMPLETED
                task.result = message
                completed.add(task.task_id)
                executions.append(
                    TaskExecution(
                        task_id=task.task_id,
                        status=TaskStatus.COMPLETED,
                        message=message,
                        artifacts=normalized_artifacts,
                        changed=True,
                        executed_at=execution_time,
                    )
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                task.status = TaskStatus.FAILED
                task.result = message
                executions.append(
                    TaskExecution(
                        task_id=task.task_id,
                        status=TaskStatus.FAILED,
                        message=message,
                        artifacts=(),
                        changed=False,
                        executed_at=execution_time,
                    )
                )

        finished_time = datetime.now(UTC)
        outcome_material = "|".join(
            f"{execution.task_id}:{execution.status.value}" for execution in executions
        )
        report_id = "report-" + hashlib.sha256(
            f"{plan.plan_id}|{execution_time.isoformat()}|{outcome_material}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        return ExecutionReport(
            report_id=report_id,
            plan_id=plan.plan_id,
            started_at=execution_time,
            finished_at=finished_time,
            executions=tuple(executions),
            deferred_task_ids=tuple(deferred),
        )


class CampaignEngine:
    """Facade coordinating validation, planning, persistence, and execution."""

    def __init__(
        self,
        store: JsonStateStore | Path | str | None = None,
        archive_index: ArchiveIndex | None = None,
        planner: CampaignPlanner | None = None,
        executor: CampaignExecutor | None = None,
    ) -> None:
        if store is None:
            self.store = JsonStateStore(Path(".hack-submission/state.json"))
        elif isinstance(store, JsonStateStore):
            self.store = store
        else:
            self.store = JsonStateStore(store)
        self.archive_index = archive_index
        self.planner = planner or CampaignPlanner(archive_index=archive_index)
        self.executor = executor or CampaignExecutor()

    def create_plan(
        self,
        submission: HackSubmission,
        request: CampaignRequest,
        *,
        created_at: datetime | None = None,
    ) -> CampaignPlan:
        return self.planner.build_plan(
            submission, request, created_at=created_at
        )

    def save_plan(self, plan: CampaignPlan) -> None:
        self.store.save_plan(plan)

    def execute(
        self,
        plan_or_id: CampaignPlan | str,
        output_dir: Path,
        *,
        now: datetime | None = None,
        allow_early: bool = False,
    ) -> ExecutionReport:
        plan = (
            plan_or_id
            if isinstance(plan_or_id, CampaignPlan)
            else self.store.get_plan(plan_or_id)
        )
        prior_reports = self.store.get_reports(plan.plan_id)
        completed_ids = {
            execution.task_id
            for report in prior_reports
            for execution in report.executions
            if execution.status is TaskStatus.COMPLETED
        }
        report = self.executor.execute(
            plan,
            output_dir,
            now=now,
            allow_early=allow_early,
            completed_task_ids=completed_ids,
        )
        self.store.record_execution(plan, report)
        return report

    def status(self, plan_id: str | None = None) -> dict[str, Any]:
        return self.store.status(plan_id)


__all__ = [
    "ArchiveIndex",
    "CampaignEngine",
    "CampaignExecutor",
    "CampaignPlanner",
    "EngineError",
    "JsonStateStore",
    "LocalArtifactDeployer",
    "MemeComposer",
    "PlanNotFoundError",
    "StateStoreError",
    "atomic_write_text",
    "calculate_visibility_score",
    "create_campaign_plan",
    "render_meme",
    "validate_submission",
]
