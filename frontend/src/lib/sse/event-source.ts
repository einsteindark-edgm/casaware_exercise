import { fetchEventSource } from "@microsoft/fetch-event-source";
import { EventEnvelopeSchema, EventEnvelope } from "../schemas/events";

export interface SSESubscription {
  close(): void;
}

export function subscribeToEvents(params: {
  url: string;
  getToken: () => Promise<string>;
  onEvent: (event: EventEnvelope) => void;
  onError?: (err: unknown) => void;
  lastEventId?: string;
}): SSESubscription {
  const abort = new AbortController();

  fetchEventSource(params.url, {
    signal: abort.signal,
    headers: {
      Accept: "text/event-stream",
      ...(params.lastEventId && { "Last-Event-ID": params.lastEventId }),
    },
    async onopen(response) {
      if (!response.ok && response.status === 401) {
        throw new Error("Unauthorized");
      }
    },
    // fetchEventSource permite un fetch custom
    fetch: async (input, init) => {
      const token = await params.getToken();
      return fetch(input, {
        ...init,
        headers: {
          ...init?.headers,
          Authorization: `Bearer ${token}`
        }
      });
    },
    onmessage(msg) {
      if (msg.event === "ping" || !msg.data) return;
      try {
        const parsed = EventEnvelopeSchema.parse(JSON.parse(msg.data));
        if (msg.id) {
          localStorage.setItem(`sse:lastEventId:${params.url}`, msg.id);
        }
        params.onEvent(parsed);
      } catch (err) {
        console.error("Failed to parse SSE message", err, msg.data);
      }
    },
    onerror(err) {
      params.onError?.(err);
      if (err instanceof Error && err.message === "Unauthorized") {
        throw err; // Stop retrying on auth failure
      }
      // Return undefined to let it retry with exponential backoff
    },
  });

  return { close: () => abort.abort() };
}
