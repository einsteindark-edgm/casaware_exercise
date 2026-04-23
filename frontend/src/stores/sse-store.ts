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
  clearEvents: () => set({ recentEvents: [] }),
}));
