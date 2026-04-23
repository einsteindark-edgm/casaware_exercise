"use client";

import { useEffect, useState } from "react";
import { useAuth } from "../auth/use-auth";
import { getBearerToken } from "../api/client";
import { subscribeToEvents } from "./event-source";
import { EventEnvelope } from "../schemas/events";
import { useSSEStore } from "@/stores/sse-store";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export function useSSEEvents(options: {
  workflowId?: string;
  enabled?: boolean;
}) {
  const { user } = useAuth();
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [localConnected, setLocalConnected] = useState(false);
  const { setConnected, addEvent, addPendingHITL, removePendingHITL } = useSSEStore();

  useEffect(() => {
    if (!user || options.enabled === false) return;

    const url = options.workflowId
      ? `${API_URL}/api/v1/workflows/${options.workflowId}/stream`
      : `${API_URL}/api/v1/events/stream`;

    const lastEventId = localStorage.getItem(`sse:lastEventId:${url}`) ?? undefined;

    const sub = subscribeToEvents({
      url,
      getToken: getBearerToken,
      lastEventId,
      onEvent: (event) => {
        setEvents((prev) => [...prev, event]);
        addEvent(event);

        // Auto-detect HITL tasks
        if (event.event_type === "workflow.hitl_required") {
          const payload = event.payload as Record<string, unknown>;
          const taskId = payload.hitl_task_id as string | undefined;
          if (taskId) {
            addPendingHITL({
              taskId,
              expenseId: event.expense_id || "",
              fieldsInConflict: (payload.fields_in_conflict as unknown[]) || [],
            });
          }
        }

        if (event.event_type === "workflow.hitl_resolved") {
          const payload = event.payload as Record<string, unknown>;
          const taskId = payload.hitl_task_id as string | undefined;
          if (taskId) removePendingHITL(taskId);
        }
      },
    });

    setLocalConnected(true);
    if (!options.workflowId) {
      setConnected(true);
    }

    return () => {
      sub.close();
      setLocalConnected(false);
      if (!options.workflowId) {
        setConnected(false);
      }
    };
  }, [user, options.workflowId, options.enabled, setConnected, addEvent, addPendingHITL, removePendingHITL]);

  return { events, connected: localConnected };
}
