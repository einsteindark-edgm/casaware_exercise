"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useSSEEvents } from "@/lib/sse/use-sse-events";
import { apiClient } from "@/lib/api/client";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CheckCircle2, Clock, AlertTriangle, XCircle, FileSearch, ArrowRight, Database, BrainCircuit } from "lucide-react";

type TimelineState = "received" | "processing" | "ocr" | "hitl" | "finalizing" | "vectorizing" | "completed" | "failed";

interface ExpenseRead {
  expense_id: string;
  status: "pending" | "processing" | "hitl_required" | "approved" | "rejected";
  workflow_id: string;
  amount: number;
  currency: string;
  date: string;
  vendor: string;
  category: string;
  receipt_id: string;
  created_at: string;
}

interface WorkflowStatus {
  state?: string;
  current_step?: string | null;
}

interface HitlTask {
  task_id: string;
  status: string;
  workflow_id: string;
}

interface ExpenseEvent {
  event_id: string;
  event_type: string;
  actor: { type: string; id: string };
  details: Record<string, unknown>;
  created_at: string;
}

const EVENT_LABELS: Record<string, string> = {
  created: "Receipt uploaded",
  ocr_started: "OCR started (Textract)",
  ocr_completed: "OCR completed",
  hitl_required: "Human review requested",
  hitl_resolved: "Human review resolved",
  completed: "Audit completed",
  failed: "Audit failed",
};

function formatEventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? eventType;
}

function formatDetails(details: Record<string, unknown>): string {
  const keys = Object.keys(details);
  if (keys.length === 0) return "";
  return keys
    .map((k) => {
      const v = details[k];
      if (typeof v === "object") return `${k}: ${JSON.stringify(v)}`;
      return `${k}: ${String(v)}`;
    })
    .join(" · ");
}

interface OcrField {
  field?: string;
  value?: unknown;
  confidence?: number;
}

interface OcrLineItem {
  item?: string;
  price?: string;
  quantity?: string;
  unit_price?: string;
  product_code?: string;
  confidence_avg?: number;
}

function confidenceColor(conf: number | undefined): string {
  if (conf == null) return "bg-gray-100 text-gray-600";
  if (conf >= 90) return "bg-green-100 text-green-700";
  if (conf >= 70) return "bg-yellow-100 text-yellow-800";
  return "bg-red-100 text-red-700";
}

function formatFieldLabel(raw: string | undefined): string {
  if (!raw) return "";
  return raw.replace(/^ocr_/, "").replace(/_/g, " ");
}

function formatValue(v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function EventDetails({ event }: { event: ExpenseEvent }) {
  const d = event.details || {};

  if (event.event_type === "ocr_completed") {
    const fields = (d.fields_summary as OcrField[] | undefined) || [];
    const lineItems = (d.line_items as OcrLineItem[] | undefined) || [];
    const avg = d.avg_confidence as number | undefined;
    return (
      <div className="mt-2 space-y-2">
        {avg != null && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Avg confidence:</span>
            <span className={`text-xs px-2 py-0.5 rounded font-medium ${confidenceColor(avg)}`}>
              {avg.toFixed(1)}%
            </span>
          </div>
        )}
        {fields.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1">
            {fields.map((f, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className="text-gray-500 capitalize min-w-[110px]">{formatFieldLabel(f.field)}:</span>
                <span className="text-gray-800 font-medium truncate">{formatValue(f.value)}</span>
                {f.confidence != null && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${confidenceColor(f.confidence)}`}>
                    {Math.round(f.confidence)}%
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
        {lineItems.length > 0 && (
          <div className="mt-2">
            <p className="text-xs text-gray-500 mb-1">Line items ({lineItems.length}):</p>
            <ul className="text-xs space-y-0.5 pl-3 border-l border-gray-200">
              {lineItems.map((li, i) => (
                <li key={i} className="flex items-center gap-2">
                  <span className="text-gray-700">{li.quantity ? `${li.quantity}× ` : ""}{li.item || "(item)"}</span>
                  {li.price && <span className="text-gray-500">— {li.price}</span>}
                  {li.confidence_avg != null && (
                    <span className={`px-1 py-0.5 rounded text-[10px] ${confidenceColor(li.confidence_avg)}`}>
                      {Math.round(li.confidence_avg)}%
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  if (event.event_type === "hitl_required") {
    const conflicts = (d.fields_in_conflict as Array<Record<string, unknown>> | undefined) || [];
    if (conflicts.length === 0) return null;
    return (
      <div className="mt-2 space-y-1">
        <p className="text-xs text-gray-500">Fields needing review:</p>
        {conflicts.map((c, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-medium text-gray-800 capitalize">{String(c.field)}:</span>
            <span className="text-gray-600">user={formatValue(c.user_value)}</span>
            <span className="text-gray-600">ocr={formatValue(c.ocr_value)}</span>
            {c.confidence != null && (
              <span className={`px-1.5 py-0.5 rounded text-[10px] ${confidenceColor(c.confidence as number)}`}>
                {Math.round(c.confidence as number)}%
              </span>
            )}
          </div>
        ))}
      </div>
    );
  }

  if (event.event_type === "approved") {
    const sources = (d.source_per_field as Record<string, string> | undefined) || {};
    const final = (d.final_data as Record<string, unknown> | undefined) || {};
    const keys = Object.keys(final);
    if (keys.length === 0) return null;
    return (
      <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-1">
        {keys.map((k) => (
          <div key={k} className="flex items-center gap-2 text-xs">
            <span className="text-gray-500 capitalize min-w-[80px]">{k}:</span>
            <span className="text-gray-800 font-medium">{formatValue(final[k])}</span>
            {sources[k] && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-700">
                {sources[k]}
              </span>
            )}
          </div>
        ))}
      </div>
    );
  }

  if (event.event_type === "ocr_started") {
    const key = d.s3_key as string | undefined;
    return key ? <p className="mt-1 text-xs text-gray-500 truncate">s3://{key}</p> : null;
  }

  if (event.event_type === "failed") {
    const reason = d.reason as string | undefined;
    const message = d.message as string | undefined;
    return (
      <div className="mt-1 text-xs text-red-700">
        {reason && <span className="font-medium">{reason}</span>}
        {message && <span className="ml-1 text-red-600">— {message}</span>}
      </div>
    );
  }

  // Fallback for unknown event types — keep the legacy behavior so we don't
  // hide data we haven't styled yet.
  const fallback = formatDetails(d);
  return fallback ? <p className="mt-1 text-xs text-gray-600 font-mono break-all">{fallback}</p> : null;
}

function stateFromExpense(status: ExpenseRead["status"]): TimelineState {
  switch (status) {
    case "pending":
    case "processing":
      return "processing";
    case "hitl_required":
      return "hitl";
    case "approved":
      return "completed";
    case "rejected":
      return "failed";
  }
}

function stateFromWorkflowStep(step: string | null | undefined): TimelineState | null {
  if (!step) return null;
  if (step === "started") return "processing";
  if (step === "ocr" || step === "ocr_extraction") return "ocr";
  if (step === "audit_validation") return "processing";
  if (step === "hitl_required" || step === "waiting_human") return "hitl";
  if (step === "hitl_resolved") return "processing";
  if (step === "finalizing" || step === "finalizing_in_mongo") return "finalizing";
  if (step === "vectorizing") return "vectorizing";
  if (step === "completed") return "completed";
  if (step === "hitl_timeout" || step === "cancelled") return "failed";
  return null;
}

const STATE_RANK: Record<TimelineState, number> = {
  received: 0,
  processing: 1,
  ocr: 2,
  hitl: 3,
  finalizing: 4,
  vectorizing: 5,
  completed: 6,
  failed: 7,
};

function maxState(a: TimelineState, b: TimelineState): TimelineState {
  if (a === "failed" || b === "failed") return "failed";
  return STATE_RANK[a] >= STATE_RANK[b] ? a : b;
}

export default function ExpenseDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;
  const workflowId = `expense-audit-${id}`;

  const { events, connected } = useSSEEvents({ workflowId });

  const [hydratedState, setHydratedState] = useState<TimelineState | null>(null);
  const [liveState, setLiveState] = useState<TimelineState | null>(null);
  const [hitlTaskId, setHitlTaskId] = useState<string | null>(null);
  const [hydrating, setHydrating] = useState(true);
  const [history, setHistory] = useState<ExpenseEvent[]>([]);
  const [expense, setExpense] = useState<ExpenseRead | null>(null);
  const [receiptUrl, setReceiptUrl] = useState<string | null>(null);
  const [receiptMime, setReceiptMime] = useState<string | null>(null);

  // Hydrate initial state from persistent backend
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const expenseData = await apiClient.get(`api/v1/expenses/${id}`).json<ExpenseRead>();
        if (cancelled) return;
        setExpense(expenseData);
        let state = stateFromExpense(expenseData.status);
        const expense = expenseData;

        // Try to refine with Temporal (may 404 if backend restarted)
        try {
          const wf = await apiClient
            .get(`api/v1/workflows/${expense.workflow_id}/status`)
            .json<WorkflowStatus>();
          if (!cancelled) {
            const wfState = stateFromWorkflowStep(wf.current_step);
            if (wf.state === "completed") state = "completed";
            else if (wf.state === "failed") state = "failed";
            else if (wfState) state = maxState(state, wfState);
          }
        } catch {
          // workflow not found → trust expense.status
        }

        // If pending HITL, recover task_id
        if (state === "hitl") {
          try {
            const tasks = await apiClient
              .get("api/v1/hitl", {
                searchParams: { workflow_id: expense.workflow_id, status: "pending" },
              })
              .json<HitlTask[]>();
            if (!cancelled && tasks.length > 0) {
              setHitlTaskId(tasks[0].task_id);
            }
          } catch (err) {
            console.warn("Failed to load pending HITL task", err);
          }
        }

        if (!cancelled) setHydratedState(state);

        try {
          const hist = await apiClient
            .get(`api/v1/expenses/${id}/history`)
            .json<ExpenseEvent[]>();
          if (!cancelled) setHistory(hist);
        } catch (err) {
          console.warn("Failed to load expense history", err);
        }
      } catch (err) {
        console.error("Failed to hydrate expense detail", err);
        if (!cancelled) setHydratedState("received");
      } finally {
        if (!cancelled) setHydrating(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Download receipt as blob (Authenticated URL → object URL for <img>/<embed>)
  useEffect(() => {
    let cancelled = false;
    let createdUrl: string | null = null;
    (async () => {
      try {
        const blob = await apiClient.get(`api/v1/expenses/${id}/receipt`).blob();
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setReceiptUrl(createdUrl);
        setReceiptMime(blob.type || null);
      } catch (err) {
        console.warn("Failed to load receipt", err);
      }
    })();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [id]);

  // Process live SSE events
  useEffect(() => {
    if (!events.length) return;
    let next: TimelineState | null = liveState;
    let shouldRefreshHistory = false;
    for (const event of events) {
      switch (event.event_type) {
        case "workflow.started":
          next = next ? maxState(next, "processing") : "processing";
          break;
        case "workflow.step_changed": {
          const stepPayload = event.payload as Record<string, unknown>;
          const step = stepPayload.step as string | undefined;
          const mapped = stateFromWorkflowStep(step);
          if (mapped) next = next ? maxState(next, mapped) : mapped;
          // Each step transition has a corresponding `expense_events` write
          // in Mongo (ocr_started, ocr_completed, hitl_required, approved,
          // failed). Refresh so the Receipt History card reflects them live
          // instead of waiting for workflow.completed.
          shouldRefreshHistory = true;
          break;
        }
        case "workflow.ocr_progress":
          next = next ? maxState(next, "ocr") : "ocr";
          shouldRefreshHistory = true;
          break;
        case "workflow.hitl_required": {
          next = next ? maxState(next, "hitl") : "hitl";
          const payload = event.payload as Record<string, unknown>;
          const taskId = payload.hitl_task_id as string | undefined;
          if (taskId) setHitlTaskId(taskId);
          shouldRefreshHistory = true;
          break;
        }
        case "workflow.hitl_resolved":
          setHitlTaskId(null);
          shouldRefreshHistory = true;
          break;
        case "workflow.completed":
          next = "completed";
          shouldRefreshHistory = true;
          break;
        case "workflow.failed":
          next = "failed";
          shouldRefreshHistory = true;
          break;
      }
    }
    if (next && next !== liveState) setLiveState(next);
    if (shouldRefreshHistory) {
      apiClient
        .get(`api/v1/expenses/${id}/history`)
        .json<ExpenseEvent[]>()
        .then(setHistory)
        .catch((err) => console.warn("Failed to refresh history", err));
    }
  }, [events, liveState, id]);

  const currentState: TimelineState = useMemo(() => {
    if (hydratedState && liveState) return maxState(hydratedState, liveState);
    return hydratedState ?? liveState ?? "received";
  }, [hydratedState, liveState]);

  type TimelineStep = {
    id: string;
    title: string;
    icon: typeof Clock;
    isWarning?: boolean;
    isSuccess?: boolean;
    isError?: boolean;
  };
  const timelineSteps: TimelineStep[] = [
    { id: "received", title: "Received", icon: Clock },
    { id: "ocr", title: "Extracting text (Textract)", icon: FileSearch },
    { id: "hitl", title: "Waiting for your review", icon: AlertTriangle, isWarning: true },
    { id: "finalizing", title: "Saving final data", icon: Database },
    { id: "vectorizing", title: "Teaching the chatbot about your receipt...", icon: BrainCircuit },
    { id: "completed", title: "Audit completed", icon: CheckCircle2, isSuccess: true },
    { id: "failed", title: "Processing error", icon: XCircle, isError: true }
  ];

  let activeStepIndex = 0;
  if (currentState === "processing") activeStepIndex = 1;
  else if (currentState === "ocr") activeStepIndex = 1;
  else if (currentState === "hitl") activeStepIndex = 2;
  else if (currentState === "finalizing") activeStepIndex = 3;
  else if (currentState === "vectorizing") activeStepIndex = 4;
  else if (currentState === "completed") activeStepIndex = 5;
  else if (currentState === "failed") activeStepIndex = 6;

  const isCompleted = currentState === "completed";

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Audit Detail</h1>
          <p className="text-gray-500">ID: {id}</p>
        </div>
        <Badge variant={connected ? "default" : "secondary"}>
          {connected ? "Live" : "Connecting..."}
        </Badge>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Expense Information</CardTitle>
            <CardDescription>Data entered when creating the audit.</CardDescription>
          </CardHeader>
          <CardContent>
            {expense ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-3 text-sm">
                <dt className="text-gray-500">Amount</dt>
                <dd className="text-gray-900 font-medium">
                  {expense.amount.toLocaleString(undefined, { minimumFractionDigits: 2 })} {expense.currency}
                </dd>
                <dt className="text-gray-500">Date</dt>
                <dd className="text-gray-900">{new Date(expense.date).toLocaleDateString()}</dd>
                <dt className="text-gray-500">Vendor</dt>
                <dd className="text-gray-900">{expense.vendor}</dd>
                <dt className="text-gray-500">Category</dt>
                <dd className="text-gray-900">{expense.category}</dd>
                <dt className="text-gray-500">Status</dt>
                <dd className="text-gray-900 capitalize">{expense.status.replace("_", " ")}</dd>
                <dt className="text-gray-500">Created</dt>
                <dd className="text-gray-900">{new Date(expense.created_at).toLocaleString()}</dd>
                <dt className="text-gray-500">Receipt</dt>
                <dd className="text-gray-700 font-mono text-xs break-all">{expense.receipt_id}</dd>
              </dl>
            ) : (
              <p className="text-sm text-gray-500">{hydrating ? "Loading..." : "Not available"}</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Uploaded Receipt</CardTitle>
            <CardDescription>Original file uploaded by the user.</CardDescription>
          </CardHeader>
          <CardContent>
            {!receiptUrl ? (
              <p className="text-sm text-gray-500">Loading receipt...</p>
            ) : receiptMime?.startsWith("image/") ? (
              <a href={receiptUrl} target="_blank" rel="noreferrer" className="block">
                <img
                  src={receiptUrl}
                  alt="Receipt"
                  className="w-full max-h-96 object-contain rounded border bg-gray-50"
                />
              </a>
            ) : receiptMime === "application/pdf" ? (
              <div className="space-y-2">
                <embed
                  src={receiptUrl}
                  type="application/pdf"
                  className="w-full h-96 rounded border bg-gray-50"
                />
                <a
                  href={receiptUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm text-blue-600 hover:underline"
                >
                  Open PDF in new tab
                </a>
              </div>
            ) : (
              <a
                href={receiptUrl}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-blue-600 hover:underline"
              >
                Download file
              </a>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Audit Progress</CardTitle>
          <CardDescription>
            {hydrating ? "Loading status..." : "Real-time events from the orchestrator."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-8">
            {timelineSteps.map((step, index) => {
              if (step.id === "failed" && currentState !== "failed") return null;

              const isPast = isCompleted ? index < activeStepIndex : index < activeStepIndex;
              const isActive = !isCompleted && index === activeStepIndex;
              const isDoneFinal = isCompleted && index === activeStepIndex;

              let iconColor = "text-gray-300";
              if (isPast || isDoneFinal) iconColor = "text-green-500";
              if (isActive) {
                if (step.isWarning) iconColor = "text-orange-500";
                else if (step.isError) iconColor = "text-red-500";
                else iconColor = "text-blue-500";
              }

              return (
                <div key={step.id} className="relative flex gap-4">
                  {index !== timelineSteps.length - 1 && (
                    <div className={`absolute left-3 top-8 bottom-[-2rem] w-px ${isPast || isDoneFinal ? 'bg-green-500' : 'bg-gray-200'}`} />
                  )}

                  <div className={`relative z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-white ring-2 ${isActive ? 'ring-blue-100' : 'ring-white'}`}>
                    <step.icon className={`h-6 w-6 ${iconColor} ${isActive && !step.isError && !step.isWarning ? 'animate-pulse' : ''}`} />
                  </div>

                  <div className="flex-1 pb-4">
                    <p className={`text-sm font-medium ${isActive || isDoneFinal ? 'text-gray-900' : isPast ? 'text-gray-700' : 'text-gray-400'}`}>
                      {step.title}
                    </p>

                    {isActive && step.id === "hitl" && hitlTaskId && (
                      <div className="mt-3 bg-orange-50 rounded-md p-4 border border-orange-100">
                        <p className="text-sm text-orange-800 mb-3">Discrepancies detected between the reported data and OCR.</p>
                        <Button
                          onClick={() => router.push(`/hitl/${hitlTaskId}`)}
                          className="bg-orange-600 hover:bg-orange-700"
                        >
                          Resolve now <ArrowRight className="ml-2 w-4 h-4" />
                        </Button>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Receipt History</CardTitle>
          <CardDescription>All states this document had, in chronological order.</CardDescription>
        </CardHeader>
        <CardContent>
          {history.length === 0 ? (
            <p className="text-sm text-gray-500">{hydrating ? "Loading history..." : "No events recorded."}</p>
          ) : (
            <ul className="space-y-4">
              {history.map((event) => (
                <li key={event.event_id} className="flex gap-3">
                  <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-blue-500" />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-baseline justify-between gap-2">
                      <p className="text-sm font-medium text-gray-900">
                        {formatEventLabel(event.event_type)}
                      </p>
                      <span className="text-xs text-gray-400 shrink-0">
                        {new Date(event.created_at).toLocaleString()}
                      </span>
                    </div>
                    <p className="text-xs text-gray-500">
                      {event.actor.type === "user" ? "User" : event.actor.type === "system" ? "System" : event.actor.type}
                      {event.actor.id ? ` · ${event.actor.id}` : ""}
                    </p>
                    <EventDetails event={event} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card className="mt-8 border-dashed border-gray-300 bg-gray-50">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm text-gray-500">Raw SSE Events (Debug)</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-xs font-mono bg-white p-4 rounded border overflow-x-auto max-h-60 overflow-y-auto">
            {events.length === 0 ? "Waiting for events..." :
              events.map((e, i) => (
                <div key={i} className="mb-2 pb-2 border-b border-gray-100 last:border-0">
                  <span className="text-blue-600">[{new Date(e.timestamp).toLocaleTimeString()}]</span>
                  <span className="font-bold text-gray-700 ml-2">{e.event_type}</span>
                  <div className="text-gray-500 mt-1">{JSON.stringify(e.payload)}</div>
                </div>
              ))
            }
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
