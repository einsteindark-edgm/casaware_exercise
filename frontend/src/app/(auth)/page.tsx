"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertCircle, ArrowRight, FileText, Loader2, Sparkles } from "lucide-react";

import { apiClient } from "@/lib/api/client";
import { useSSEStore, HITLTask } from "@/stores/sse-store";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button, buttonVariants } from "@/components/ui/button";

interface ExpenseSummary {
  expense_id: string;
  status: "pending" | "processing" | "hitl_required" | "approved" | "rejected";
  amount: number;
  currency: string;
  vendor: string;
  final_amount?: number | null;
  final_currency?: string | null;
  workflow_id: string;
  created_at: string;
}

interface ExpenseListResponse {
  items: ExpenseSummary[];
  next_cursor: string | null;
}

interface HitlTaskApi {
  task_id: string;
  expense_id: string | null;
  workflow_id: string;
  status: string;
  fields_in_conflict: Array<Record<string, unknown>>;
  created_at: string;
}

function formatCurrencySum(totals: Record<string, number>): string {
  const entries = Object.entries(totals).filter(([, v]) => v > 0);
  if (entries.length === 0) return "—";
  return entries
    .map(
      ([ccy, amt]) =>
        `${amt.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${ccy}`
    )
    .join(" · ");
}

function shortId(id: string): string {
  const raw = id.replace(/^expense_/, "");
  return raw.length > 10 ? `${raw.slice(0, 10)}…` : raw;
}

export default function DashboardPage() {
  const pendingHITL = useSSEStore((state) => state.pendingHITL);
  const recentEvents = useSSEStore((state) => state.recentEvents);
  const setPendingHITL = useSSEStore((state) => state.setPendingHITL);

  const [approved, setApproved] = useState<ExpenseSummary[]>([]);
  const [inFlight, setInFlight] = useState<ExpenseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const [approvedRes, pendingRes, processingRes, hitlRes] =
          await Promise.all([
            apiClient
              .get("api/v1/expenses?status=approved&limit=200")
              .json<ExpenseListResponse>(),
            apiClient
              .get("api/v1/expenses?status=pending&limit=50")
              .json<ExpenseListResponse>(),
            apiClient
              .get("api/v1/expenses?status=processing&limit=50")
              .json<ExpenseListResponse>(),
            apiClient
              .get("api/v1/hitl?status=pending")
              .json<HitlTaskApi[]>(),
          ]);
        if (cancelled) return;

        setApproved(approvedRes.items);
        const merged = [...pendingRes.items, ...processingRes.items].sort(
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        );
        setInFlight(merged);

        const hydrated: HITLTask[] = hitlRes.map((t) => ({
          taskId: t.task_id,
          expenseId: t.expense_id ?? "",
          fieldsInConflict: t.fields_in_conflict ?? [],
        }));
        setPendingHITL(hydrated);
        setError(null);
      } catch (err) {
        if (!cancelled) {
          console.error("Dashboard hydration failed", err);
          setError("Could not load dashboard data.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [setPendingHITL]);

  // Latest known workflow step per expense, derived from live SSE events.
  const stepByExpense = useMemo(() => {
    const out: Record<string, string> = {};
    for (const ev of recentEvents) {
      if (!ev.expense_id) continue;
      if (ev.event_type === "workflow.step_changed") {
        const step = (ev.payload as Record<string, unknown>)?.step;
        if (typeof step === "string") out[ev.expense_id] = step;
      }
    }
    return out;
  }, [recentEvents]);

  const auditedTotals = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const e of approved) {
      const ccy = (e.final_currency || e.currency || "").toUpperCase();
      const amt =
        typeof e.final_amount === "number" ? e.final_amount : e.amount ?? 0;
      if (!ccy || !amt) continue;
      totals[ccy] = (totals[ccy] ?? 0) + amt;
    }
    return totals;
  }, [approved]);

  const vectorizingCount = useMemo(
    () =>
      inFlight.filter((e) => stepByExpense[e.expense_id] === "vectorizing")
        .length,
    [inFlight, stepByExpense]
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">
          Dashboard
        </h1>
        <p className="text-gray-500">
          General overview of your activity and audits.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">
              Audited Receipts
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{approved.length}</div>
            <p className="text-xs text-muted-foreground">
              {formatCurrencySum(auditedTotals)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">In Progress</CardTitle>
            <Loader2
              className={`h-4 w-4 text-blue-500 ${inFlight.length > 0 ? "animate-spin" : ""}`}
            />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{inFlight.length}</div>
            <p className="text-xs text-muted-foreground">
              {vectorizingCount > 0
                ? `${vectorizingCount} vectorizing`
                : "Waiting on OCR / audit"}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Pending HITL</CardTitle>
            <AlertCircle
              className={`h-4 w-4 ${pendingHITL.length > 0 ? "text-orange-500" : "text-gray-400"}`}
            />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{pendingHITL.length}</div>
            <p className="text-xs text-muted-foreground">
              {pendingHITL.length === 0
                ? "No human review needed"
                : "Awaiting your review"}
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2 border-orange-200 bg-orange-50/30">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-orange-800">
              <AlertCircle className="h-5 w-5" />
              Pending your review
            </CardTitle>
            <CardDescription className="text-orange-600/80">
              Audits stopped because OCR found a discrepancy with what the user
              reported.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {loading ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                Loading…
              </p>
            ) : pendingHITL.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No pending tasks. Everything is in order.
              </p>
            ) : (
              <div className="space-y-2">
                {pendingHITL.map((task) => (
                  <div
                    key={task.taskId}
                    className="flex items-center justify-between bg-white p-3 rounded-md border border-orange-100 shadow-sm"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        Expense {shortId(task.expenseId || task.taskId)}
                      </p>
                      <p className="text-xs text-gray-500">
                        {task.fieldsInConflict.length} field
                        {task.fieldsInConflict.length === 1 ? "" : "s"} in
                        conflict
                      </p>
                    </div>
                    <Link
                      href={`/hitl/${task.taskId}`}
                      className={buttonVariants({
                        size: "sm",
                        variant: "outline",
                        className:
                          "border-orange-200 text-orange-700 hover:bg-orange-50",
                      })}
                    >
                      Resolve
                    </Link>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Quick Actions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Link
              href="/expenses/new"
              className={buttonVariants({ className: "w-full justify-start" })}
            >
              <FileText className="mr-2 h-4 w-4" />
              Upload new receipt
            </Link>
            <Link
              href="/expenses"
              className={buttonVariants({
                variant: "outline",
                className: "w-full justify-start",
              })}
            >
              <ArrowRight className="mr-2 h-4 w-4" />
              View all expenses
            </Link>
          </CardContent>
        </Card>
      </div>

      <Card className="border-blue-200 bg-blue-50/30">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-blue-800">
            <Sparkles className="h-5 w-5" />
            Audit pipeline
          </CardTitle>
          <CardDescription className="text-blue-600/80">
            Receipts currently moving through OCR, audit and vector indexing.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              Loading…
            </p>
          ) : inFlight.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              Nothing in flight right now.
            </p>
          ) : (
            <div className="divide-y divide-blue-100 rounded border border-blue-100 bg-white">
              {inFlight.map((e) => {
                const step = stepByExpense[e.expense_id];
                const isVectorizing = step === "vectorizing";
                return (
                  <div
                    key={e.expense_id}
                    className="flex items-center justify-between gap-3 px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        {e.vendor || "(no vendor)"}
                      </p>
                      <p className="text-xs text-gray-500 font-mono truncate">
                        {shortId(e.expense_id)}
                      </p>
                    </div>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide ${
                        isVectorizing
                          ? "bg-purple-100 text-purple-800"
                          : e.status === "processing"
                            ? "bg-blue-100 text-blue-800"
                            : "bg-gray-100 text-gray-700"
                      }`}
                    >
                      {step ? step.replace(/_/g, " ") : e.status}
                    </span>
                    <Link
                      href={`/expenses/${e.expense_id}`}
                      className={buttonVariants({
                        size: "sm",
                        variant: "ghost",
                      })}
                    >
                      View
                    </Link>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
