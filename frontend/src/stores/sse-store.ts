import { create } from "zustand";
import { EventEnvelope } from "@/lib/schemas/events";

export interface HITLTask {
  taskId: string;
  expenseId: string;
  fieldsInConflict: any[];
}

interface SSEState {
  connected: boolean;
  recentEvents: EventEnvelope[];
  pendingHITL: HITLTask[];
  setConnected: (status: boolean) => void;
  addEvent: (event: EventEnvelope) => void;
  addPendingHITL: (task: HITLTask) => void;
  removePendingHITL: (taskId: string) => void;
  setPendingHITL: (tasks: HITLTask[]) => void;
  clearEvents: () => void;
}

export const useSSEStore = create<SSEState>((set) => ({
  connected: false,
  recentEvents: [],
  pendingHITL: [],
  setConnected: (status) => set({ connected: status }),
  addEvent: (event) => set((state) => ({ recentEvents: [...state.recentEvents, event] })),
  addPendingHITL: (task) => set((state) => ({
    // Avoid duplicates
    pendingHITL: state.pendingHITL.find(t => t.taskId === task.taskId)
      ? state.pendingHITL
      : [...state.pendingHITL, task]
  })),
  removePendingHITL: (taskId) => set((state) => ({
    pendingHITL: state.pendingHITL.filter(t => t.taskId !== taskId)
  })),
  setPendingHITL: (tasks) => set((state) => {
    // Merge: keep any live SSE entries already present that the API hasn't
    // returned yet (race between hydration and a fresh hitl_required event).
    const map = new Map<string, HITLTask>();
    for (const t of tasks) map.set(t.taskId, t);
    for (const t of state.pendingHITL) if (!map.has(t.taskId)) map.set(t.taskId, t);
    return { pendingHITL: Array.from(map.values()) };
  }),
  clearEvents: () => set({ recentEvents: [] }),
}));
