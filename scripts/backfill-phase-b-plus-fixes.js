// ────────────────────────────────────────────────────────────────────
// Backfill idempotente para los fixes F1, F2, F6 de
// infra/plans/fix-hitl-ocr-propagation.md
//
// Uso:
//   mongosh "$MONGODB_URI" --file scripts/backfill-phase-b-plus-fixes.js
//
// Tres operaciones (cada una salta docs ya corregidos):
//   1. hitl_tasks.fields_in_conflict → discrepancy_fields  (rename de key)
//   2. hitl_tasks.{decision, resolved_fields, resolved_by} reconstruidos
//      desde expense_events tipo "hitl_resolved"
//   3. receipts.expense_id reconstruido desde expenses.receipt_id
// ────────────────────────────────────────────────────────────────────

use("nexus_dev");

print("═══ Backfill Phase B+ fixes ═══");
print("Target DB:", db.getName(), "at", db.getMongo());
print();

// ── 1. Rename hitl_tasks.fields_in_conflict → discrepancy_fields ─────
const r1 = db.hitl_tasks.updateMany(
  { fields_in_conflict: { $exists: true } },
  { $rename: { fields_in_conflict: "discrepancy_fields" } }
);
print(`[1/3] rename fields_in_conflict → discrepancy_fields: ${r1.modifiedCount} docs patched`);

// ── 2. Backfill hitl_tasks.{decision, resolved_fields, resolved_by} ──
const evCursor = db.expense_events.find({ event_type: "hitl_resolved" });
let hitlPatched = 0;
while (evCursor.hasNext()) {
  const ev = evCursor.next();
  const taskId = ev.details && ev.details.hitl_task_id;
  if (!taskId) continue;
  const r = db.hitl_tasks.updateOne(
    { task_id: taskId, decision: { $exists: false } },
    {
      $set: {
        decision: ev.details.decision,
        resolved_fields: ev.details.resolved_fields,
        resolved_by: ev.actor && ev.actor.id,
      },
    }
  );
  hitlPatched += r.modifiedCount;
}
print(`[2/3] backfill hitl_tasks.{decision,resolved_fields,resolved_by}: ${hitlPatched} docs patched`);

// ── 3. Backfill receipts.expense_id desde expenses.receipt_id ────────
const expCursor = db.expenses.find(
  { receipt_id: { $exists: true, $ne: null } },
  { expense_id: 1, receipt_id: 1, _id: 0 }
);
let receiptsPatched = 0;
while (expCursor.hasNext()) {
  const e = expCursor.next();
  const r = db.receipts.updateOne(
    { receipt_id: e.receipt_id, expense_id: { $exists: false } },
    { $set: { expense_id: e.expense_id } }
  );
  receiptsPatched += r.modifiedCount;
}
print(`[3/3] backfill receipts.expense_id: ${receiptsPatched} docs patched`);

print();
print("═══ Backfill complete ═══");
print(`Totals: hitl rename=${r1.modifiedCount}, hitl fields=${hitlPatched}, receipts=${receiptsPatched}`);
