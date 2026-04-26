// Recover expenses that were rolled back to status="rejected" by the old
// _fail-on-vector-sync-failure path in ExpenseAuditWorkflow.
//
// Symptom: HITL was approved, the workflow successfully wrote
// status="approved" + final_*, then `trigger_vector_sync` either failed
// or timed out polling gold.expense_chunks. The previous code called
// _fail → update_expense_to_rejected, overwriting status with "rejected"
// and stamping rejection_reason="VECTOR_SYNC_TIMEOUT" or
// "VECTOR_SYNC_FAILED". Because gold.expense_audit filters
// `status='approved'`, those expenses can never reach gold.
//
// This script reverses ONLY that specific transition. It targets:
//   - status="rejected"
//   - rejection_reason in ("VECTOR_SYNC_TIMEOUT","VECTOR_SYNC_FAILED")
//   - final_amount IS NOT NULL  (proof that update_expense_to_approved ran first)
//
// It does NOT touch genuine rejections (cancellations, HITL timeouts,
// fraud, etc). Idempotent — running it twice on the same data is a no-op.
//
// Usage:
//   mongosh "$MONGODB_URI" scripts/recover-vector-sync-rejected.js
//
// or to dry-run:
//   DRY_RUN=1 mongosh "$MONGODB_URI" scripts/recover-vector-sync-rejected.js

const DB = "nexus_dev";
const dryRun = (typeof process !== "undefined" && process.env.DRY_RUN === "1");

const filter = {
  status: "rejected",
  rejection_reason: { $in: ["VECTOR_SYNC_TIMEOUT", "VECTOR_SYNC_FAILED"] },
  final_amount: { $ne: null },
};

const candidates = db.getSiblingDB(DB).expenses
  .find(filter, {
    expense_id: 1, tenant_id: 1, status: 1, rejection_reason: 1,
    final_amount: 1, final_currency: 1, final_vendor: 1, final_date: 1,
    approved_at: 1, updated_at: 1, _id: 0,
  })
  .toArray();

print(`found ${candidates.length} expense(s) to recover (dryRun=${dryRun})`);
candidates.forEach((d) => printjson(d));

if (dryRun || candidates.length === 0) {
  print("done (no writes performed)");
  quit();
}

const ids = candidates.map((c) => c.expense_id);
const now = new Date();

// Restore status to approved and clear the rejection_reason that the
// old _fail path stamped on. Leave final_*, source_per_field, approved_at
// alone — those are the human-resolved values we want preserved.
const res = db.getSiblingDB(DB).expenses.updateMany(
  { expense_id: { $in: ids } },
  {
    $set: { status: "approved", updated_at: now },
    $unset: { rejection_reason: "" },
  }
);

print(`matched=${res.matchedCount}, modified=${res.modifiedCount}`);

// Append a recovery breadcrumb to expense_events so the timeline shows
// what happened.
const events = ids.map((expense_id) => ({
  event_id: `evt_recover_${expense_id}_${now.getTime()}`,
  expense_id,
  tenant_id: candidates.find((c) => c.expense_id === expense_id).tenant_id,
  event_type: "vector_sync_failed",
  actor: { type: "system", id: "ops:recover-vector-sync-rejected" },
  details: {
    note: "rolled back from rejected→approved; original _fail path stamped this row erroneously",
    prior_rejection_reason: candidates.find((c) => c.expense_id === expense_id).rejection_reason,
  },
  workflow_id: null,
  metadata: {},
  created_at: now,
}));
db.getSiblingDB(DB).expense_events.insertMany(events);
print(`appended ${events.length} timeline event(s)`);
print("done");
