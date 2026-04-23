"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useSSEEvents } from "@/lib/sse/use-sse-events";
import { apiClient } from "@/lib/api/client";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CheckCircle2, Clock, AlertTriangle, XCircle, FileSearch, ArrowRight } from "lucide-react";

type TimelineState = "received" | "processing" | "ocr" | "hitl" | "completed" | "failed";

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
  created: "Recibo cargado",
  ocr_started: "OCR iniciado (Textract)",
  ocr_completed: "OCR completado",
  hitl_required: "Se solicitó revisión humana",
  hitl_resolved: "Revisión humana resuelta",
  completed: "Auditoría completada",
  failed: "Auditoría falló",
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
  if (step === "ocr") return "ocr";
  if (step === "hitl_required") return "hitl";
  if (step === "hitl_resolved") return "processing";
  if (step === "completed") return "completed";
  if (step === "hitl_timeout" || step === "cancelled") return "failed";
  return null;
}

const STATE_RANK: Record<TimelineState, number> = {
  received: 0,
  processing: 1,
  ocr: 2,
  hitl: 3,
  completed: 4,
  failed: 5,
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

  // Hidratar estado inicial desde backend persistente
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const expenseData = await apiClient.get(`api/v1/expenses/${id}`).json<ExpenseRead>();
        if (cancelled) return;
        setExpense(expenseData);
        let state = stateFromExpense(expenseData.status);
        const expense = expenseData;

        // Intentar refinar con Temporal (puede 404 si el backend reinició)
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
          // workflow no encontrado → confiamos en expense.status
        }

        // Si hay HITL pendiente, recuperar el task_id
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

  // Descargar el recibo como blob (URL autenticada → object URL para <img>/<embed>)
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

  // Procesar eventos SSE en vivo
  useEffect(() => {
    if (!events.length) return;
    let next: TimelineState | null = liveState;
    let shouldRefreshHistory = false;
    for (const event of events) {
      switch (event.event_type) {
        case "workflow.started":
          next = next ? maxState(next, "processing") : "processing";
          break;
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

  const timelineSteps = [
    { id: "received", title: "Recibido", icon: Clock },
    { id: "ocr", title: "Extrayendo texto (Textract)", icon: FileSearch },
    { id: "hitl", title: "Esperando tu revisión", icon: AlertTriangle, isWarning: true },
    { id: "completed", title: "Auditoría completada", icon: CheckCircle2, isSuccess: true },
    { id: "failed", title: "Error en proceso", icon: XCircle, isError: true }
  ] as const;

  let activeStepIndex = 0;
  if (currentState === "processing") activeStepIndex = 1;
  else if (currentState === "ocr") activeStepIndex = 1;
  else if (currentState === "hitl") activeStepIndex = 2;
  else if (currentState === "completed") activeStepIndex = 3;
  else if (currentState === "failed") activeStepIndex = 4;

  const isCompleted = currentState === "completed";

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Detalle de Auditoría</h1>
          <p className="text-gray-500">ID: {id}</p>
        </div>
        <Badge variant={connected ? "default" : "secondary"}>
          {connected ? "En vivo" : "Conectando..."}
        </Badge>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Información del gasto</CardTitle>
            <CardDescription>Datos ingresados al crear la auditoría.</CardDescription>
          </CardHeader>
          <CardContent>
            {expense ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-3 text-sm">
                <dt className="text-gray-500">Monto</dt>
                <dd className="text-gray-900 font-medium">
                  {expense.amount.toLocaleString(undefined, { minimumFractionDigits: 2 })} {expense.currency}
                </dd>
                <dt className="text-gray-500">Fecha</dt>
                <dd className="text-gray-900">{new Date(expense.date).toLocaleDateString()}</dd>
                <dt className="text-gray-500">Vendor</dt>
                <dd className="text-gray-900">{expense.vendor}</dd>
                <dt className="text-gray-500">Categoría</dt>
                <dd className="text-gray-900">{expense.category}</dd>
                <dt className="text-gray-500">Estado</dt>
                <dd className="text-gray-900 capitalize">{expense.status.replace("_", " ")}</dd>
                <dt className="text-gray-500">Creado</dt>
                <dd className="text-gray-900">{new Date(expense.created_at).toLocaleString()}</dd>
                <dt className="text-gray-500">Recibo</dt>
                <dd className="text-gray-700 font-mono text-xs break-all">{expense.receipt_id}</dd>
              </dl>
            ) : (
              <p className="text-sm text-gray-500">{hydrating ? "Cargando..." : "No disponible"}</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recibo cargado</CardTitle>
            <CardDescription>Archivo original enviado por el usuario.</CardDescription>
          </CardHeader>
          <CardContent>
            {!receiptUrl ? (
              <p className="text-sm text-gray-500">Cargando recibo...</p>
            ) : receiptMime?.startsWith("image/") ? (
              <a href={receiptUrl} target="_blank" rel="noreferrer" className="block">
                <img
                  src={receiptUrl}
                  alt="Recibo"
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
                  Abrir PDF en nueva pestaña
                </a>
              </div>
            ) : (
              <a
                href={receiptUrl}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-blue-600 hover:underline"
              >
                Descargar archivo
              </a>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Progreso de la Auditoría</CardTitle>
          <CardDescription>
            {hydrating ? "Cargando estado..." : "Eventos en tiempo real desde el orchestrator."}
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
                        <p className="text-sm text-orange-800 mb-3">Se detectaron discrepancias entre lo reportado y el OCR.</p>
                        <Button
                          onClick={() => router.push(`/hitl/${hitlTaskId}`)}
                          className="bg-orange-600 hover:bg-orange-700"
                        >
                          Resolver ahora <ArrowRight className="ml-2 w-4 h-4" />
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
          <CardTitle>Historial del recibo</CardTitle>
          <CardDescription>Todos los estados que tuvo este documento, en orden cronológico.</CardDescription>
        </CardHeader>
        <CardContent>
          {history.length === 0 ? (
            <p className="text-sm text-gray-500">{hydrating ? "Cargando historial..." : "Sin eventos registrados."}</p>
          ) : (
            <ul className="space-y-4">
              {history.map((event) => {
                const detailsStr = formatDetails(event.details || {});
                return (
                  <li key={event.event_id} className="flex gap-3">
                    <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-blue-500" />
                    <div className="flex-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <p className="text-sm font-medium text-gray-900">
                          {formatEventLabel(event.event_type)}
                        </p>
                        <span className="text-xs text-gray-400 shrink-0">
                          {new Date(event.created_at).toLocaleString()}
                        </span>
                      </div>
                      <p className="text-xs text-gray-500">
                        {event.actor.type === "user" ? "Usuario" : event.actor.type === "system" ? "Sistema" : event.actor.type}
                        {event.actor.id ? ` · ${event.actor.id}` : ""}
                      </p>
                      {detailsStr && (
                        <p className="mt-1 text-xs text-gray-600 font-mono break-all">{detailsStr}</p>
                      )}
                    </div>
                  </li>
                );
              })}
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
            {events.length === 0 ? "Esperando eventos..." :
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
