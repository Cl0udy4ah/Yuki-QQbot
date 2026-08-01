"""Live Memory V2 worker composed from the shared extraction and claim pipelines."""

from __future__ import annotations

import asyncio
import logging

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import MemoryProcessingSource, MemoryRebuildJobOutcome
from qq_ai_bot.memory.event_extractor import MemoryEventExtractor
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryJob
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.structured import StructuredTaskError
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)


class MemoryWorker:
    """Claim batches for efficiency while processing every event independently."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: MemoryJobRepository,
        facts: MemoryFactService,
        ledger: EventLedgerRepository,
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager,
        validator: MemoryClaimValidator | None = None,
        runtime_config: RuntimeConfigService | None = None,
        candidate_resolver: MemoryConflictCandidateResolver | None = None,
        relation_classifier: MemoryRelationClassifier | None = None,
        resolution_policy: MemoryResolutionPolicy | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
        extractor: MemoryEventExtractor | None = None,
        processor: MemoryClaimProcessor | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._facts = facts
        self._ledger = ledger
        models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._concurrency = concurrency
        self.metrics = metrics or MemoryLifecycleMetrics()
        candidates = candidate_resolver or MemoryConflictCandidateResolver(
            facts.repository,
            limit=settings.memory_consolidation_candidate_limit,
        )
        self.extractor = extractor or MemoryEventExtractor(models, concurrency)
        self.processor = processor or MemoryClaimProcessor(
            settings=settings,
            facts=facts,
            candidate_resolver=candidates,
            relation_classifier=relation_classifier
            or MemoryRelationClassifier(
                model_executor=models,
                concurrency=concurrency,
                max_output_tokens=settings.memory_consolidation_max_output_tokens,
            ),
            resolution_policy=resolution_policy or MemoryResolutionPolicy(),
            validator=validator,
            runtime_config=runtime_config,
            metrics=self.metrics,
        )
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._queued_since_wake = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-v2-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    async def enqueue(self, event_id: int, conversation_key: str) -> bool:
        created = await self._jobs.enqueue(event_id, conversation_key)
        if created:
            self._queued_since_wake += 1
            if self._queued_since_wake >= self._settings.memory_batch_trigger_count:
                self._queued_since_wake = 0
                self._wake.set()
        return created

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._settings.memory_batch_seconds
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set():
                await self.process_once()

    async def process_once(self) -> int:
        jobs = await self._jobs.claim(limit=self._settings.memory_batch_max_events)
        completed = 0
        for job in jobs:
            try:
                outcome = await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except (
                LLMError,
                StructuredTaskError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "memory_v2_job_failed job_id=%d event_id=%d exception_category=%s",
                    job.id,
                    job.event_id,
                    type(exc).__name__,
                )
                await self._jobs.fail(job.id, type(exc).__name__)
                continue
            await self._jobs.complete(job.id, outcome=outcome)
            completed += 1
        return completed

    async def _process_job(self, job: MemoryJob) -> MemoryRebuildJobOutcome:
        if not job.event.content.strip():
            return MemoryRebuildJobOutcome.NO_CLAIMS
        context = await self._ledger.list_before(job.event, limit=8)
        extracted = await self.extractor.extract(job.event, context=context)
        applied = 0
        for claim in extracted.output.claims:
            self.metrics.increment("claims_extracted")
            validated = self.processor.validate(claim, job.event)
            if validated is None:
                continue
            self.metrics.increment(f"claims_{claim.operation.value}ed")
            if validated.fact.authority.value == "third_party":
                self.metrics.increment("claims_third_party")
            result = await self.processor.process(
                validated,
                MemoryProcessingContext(
                    source=MemoryProcessingSource.LIVE,
                    event=job.event,
                ),
            )
            if result.fact_id is not None:
                applied += 1
        return (
            MemoryRebuildJobOutcome.CLAIMS_APPLIED if applied else MemoryRebuildJobOutcome.NO_CLAIMS
        )
