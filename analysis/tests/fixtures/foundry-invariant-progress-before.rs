            if let Some(progress) = progress {
                progress.inc(1);
                campaign_state.sync_handler_failures(&invariant_test.test_data.failures);
                // Display current best value, corpus metrics, and failure counts.
                let best = invariant_test.test_data.optimization_best_value;
                let failure_metrics = campaign_state.failure_metrics();
                let broken = failure_metrics.unique_failures.len();
                let handler_bugs = failure_metrics.broken_handlers;
                let total_invariants = invariant_contract.invariant_fns.len();
                if edge_coverage_enabled || best.is_some() || broken > 0 || handler_bugs > 0 {
                    let mut msg = String::new();
                    if let Some(best) = best {
                        msg.push_str(&format!("best: {best}"));
                    }
                    let msg =
                        if worker_count > 1 { format!("[w{}] {msg}", plan.worker_id) } else { msg };
                    progress.set_message(msg);
                }
            } else if edge_coverage_enabled
                && campaign_state.should_emit_metrics_report(DURATION_BETWEEN_METRICS_REPORT)
            {
                campaign_state.sync_handler_failures(&invariant_test.test_data.failures);
                let failure_metrics = campaign_state.failure_metrics();
                let (total_txs, total_gas) = campaign_state.throughput_totals();
                let throughput = InvariantThroughputMetrics { total_txs, total_gas };
                // Display corpus metrics inline as JSON.
                let metrics = build_invariant_progress_json(
