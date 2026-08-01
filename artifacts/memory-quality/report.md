# Memory V2 Quality Report

- Suite: `memory-v2-quality-v1` / `full`
- Commit: `d1ab3189b30811f5e5d5c19858e008675ceab670`
- Dataset: `6d2711f8d3882fefcbca82a39a073fd5f8e3c22cd26ffb36f89353170b0a3f15`
- Cases: 18/18 passed
- Failed IDs: none
- Duration: 2.919s

| Metric | Value | Numerator/denominator |
|---|---:|---:|
| `average_consolidation_requests_per_claim` | 0.0 | 0/12 |
| `average_context_characters` | 212.0 | 212/1 |
| `average_extraction_requests_per_event` | 1.0 | 14/14 |
| `average_query_embedding_requests_per_query` | 0.05263157894736842 | 1/19 |
| `blank_evidence_rate` | 0.0 | 0/1 |
| `bot_evidence_rate` | 0.0 | 0/2 |
| `bot_subject_rate` | 0.0 | 0/2 |
| `case_pass_rate` | 1.0 | 18/18 |
| `conflict_coactivation_rate` | 0.0 | 0/1 |
| `conflict_resolution_accuracy` | 1.0 | 1/1 |
| `contested_context_leak_rate` | 0.0 | 0/1 |
| `context_latency_p50_ms` | 0.029599999834317714 | 0.0296/1 |
| `context_latency_p95_ms` | null | 0/0 |
| `context_precision` | 1.0 | 14/14 |
| `context_recall` | 1.0 | 14/14 |
| `correction_resolution_accuracy` | 1.0 | 1/1 |
| `cross_group_contamination_rate` | 0.0 | 0/7 |
| `cross_person_contamination_rate` | 0.0 | 0/4 |
| `duplicate_active_fact_rate` | 0.0 | 0/19 |
| `duplicate_evidence_rate` | 0.0 | 0/12 |
| `empty_query_fact_leak_rate` | 0.0 | 0/1 |
| `evidence_provenance_accuracy` | 1.0 | 12/12 |
| `extraction_latency_p50_ms` | 11.777100000472274 | 11.7771/1 |
| `extraction_latency_p95_ms` | null | 0/0 |
| `fact_accuracy` | 1.0 | 23/23 |
| `fact_state_accuracy` | 1.0 | 23/23 |
| `fact_without_evidence_rate` | 0.0 | 0/10 |
| `historical_regression_rate` | 0.0 | 0/1 |
| `idempotency_failure_rate` | 0.0 | 0/1 |
| `mean_reciprocal_rank` | 1.0 | 1/1 |
| `ndcg_at_k` | 1.0 | 1/1 |
| `outbound_evidence_rate` | 0.0 | 0/1 |
| `pipeline_error_rate` | 0.0 | 0/18 |
| `precision_at_k` | 1.0 | 14/14 |
| `quality_suite_total_ms` | 511.0454999994545 | 511.045/1 |
| `rebuild_duplicate_commit_rate` | 0.0 | 0/1 |
| `rebuild_historical_overwrite_rate` | 0.0 | 0/1 |
| `rebuild_receipt_accuracy` | 1.0 | 1/1 |
| `rebuild_resume_accuracy` | null | 0/0 |
| `rebuild_review_bypass_rate` | 0.0 | 0/1 |
| `recall_at_k` | 1.0 | 14/14 |
| `retraction_resolution_accuracy` | null | 0/0 |
| `retrieval_latency_p50_ms` | 5.349700000806479 | 5.3497/1 |
| `retrieval_latency_p95_ms` | null | 0/0 |
| `scope_attribution_accuracy` | 1.0 | 12/12 |
| `source_event_mismatch_rate` | 0.0 | 0/12 |
| `subject_attribution_accuracy` | 1.0 | 12/12 |
| `third_party_global_leak_rate` | 0.0 | 0/1 |
| `third_party_misattribution_rate` | 0.0 | 0/1 |
| `total_model_requests` | 14.0 | 14/1 |
| `total_query_embedding_requests` | 1.0 | 1/1 |
| `unknown_subject_acceptance_rate` | 0.0 | 0/1 |
| `wrong_group_context_rate` | 0.0 | 0/14 |
| `wrong_subject_context_rate` | 0.0 | 0/14 |
| `wrong_target_retrieval_rate` | 0.0 | 0/14 |
