"use client";

import { useState, useRef, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiClient, getBearerToken } from "@/lib/api/client";
import { subscribeToEvents } from "@/lib/sse/event-source";
import { User, Bot, Send, Loader2 } from "lucide-react";
import { CitationChip, type Citation } from "@/components/chat/CitationChip";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
}

interface ChatStartResponse {
  session_id: string;
  workflow_id: string;
  turn: number;
}

interface ChatSessionHistoryResponse {
  session_id: string;
  tenant_id: string;
  turns: {
    turn: number;
    user_message: string;
    assistant_message: string;
    citations: Citation[];
    created_at?: string | null;
  }[];
}

const SESSION_STORAGE_KEY = "nexus.chat.session_id";

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);

  // Restore previous session on mount (if any).
  useEffect(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(SESSION_STORAGE_KEY) : null;
    if (!stored) return;
    setSessionId(stored);
    (async () => {
      try {
        const history = await apiClient
          .get(`api/v1/chat/sessions/${stored}`)
          .json<ChatSessionHistoryResponse>();
        const restored: ChatMessage[] = [];
        for (const t of history.turns) {
          restored.push({ id: `u-${t.turn}`, role: "user", content: t.user_message });
          restored.push({
            id: `a-${t.turn}`,
            role: "assistant",
            content: t.assistant_message,
            citations: t.citations || [],
          });
        }
        setMessages(restored);
      } catch (err) {
        console.warn("chat: could not restore session", err);
        window.localStorage.removeItem(SESSION_STORAGE_KEY);
        setSessionId(null);
      }
    })();
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage: ChatMessage = { id: `u-${Date.now()}`, role: "user", content: input };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsLoading(true);

    const asstMsgId = `a-${Date.now() + 1}`;
    setMessages((prev) => [...prev, { id: asstMsgId, role: "assistant", content: "" }]);

    try {
      const response = await apiClient
        .post("api/v1/chat", { json: { message: userMessage.content, session_id: sessionId } })
        .json<ChatStartResponse>();

      if (response.session_id) {
        setSessionId(response.session_id);
        if (typeof window !== "undefined") {
          window.localStorage.setItem(SESSION_STORAGE_KEY, response.session_id);
        }
      }

      const workflowId = response.workflow_id;

      const sub = subscribeToEvents({
        url: `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/v1/chat/stream/${workflowId}`,
        getToken: getBearerToken,
        onEvent: (event) => {
          if (event.event_type === "chat.token") {
            const token = (event.payload as { token?: string }).token || "";
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === asstMsgId ? { ...msg, content: msg.content + token } : msg,
              ),
            );
          } else if (event.event_type === "chat.complete") {
            const payload = event.payload as { citations?: Citation[]; final_text?: string };
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === asstMsgId
                  ? {
                      ...msg,
                      citations: payload.citations || [],
                      // If we missed any tokens, fall back to final_text.
                      content: msg.content || payload.final_text || "",
                    }
                  : msg,
              ),
            );
            setIsLoading(false);
            sub.close();
          }
        },
        onError: (err) => {
          console.error("Chat SSE error", err);
          setIsLoading(false);
        },
      });
    } catch (err) {
      console.error("chat: start request failed", err);
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === asstMsgId
            ? { ...msg, content: "No pude conectar con el backend. Intenta de nuevo en un momento." }
            : msg,
        ),
      );
      setIsLoading(false);
    }
  };

  const handleNewSession = () => {
    setSessionId(null);
    setMessages([]);
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(SESSION_STORAGE_KEY);
    }
  };

  return (
    <div className="h-[calc(100vh-120px)] max-h-[800px] flex flex-col mx-auto max-w-4xl border border-gray-200 rounded-lg bg-white shadow-sm overflow-hidden">
      <div className="bg-white border-b px-6 py-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-gray-900">Nexus Assistant</h1>
          <p className="text-sm text-gray-500">
            Pregunta sobre tus recibos. Devuelve totales, filtros exactos o coincidencias semánticas con enlace a cada recibo.
          </p>
        </div>
        {sessionId && (
          <Button variant="outline" size="sm" onClick={handleNewSession}>
            Nueva sesión
          </Button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-6 bg-gray-50" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center space-y-4">
            <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center">
              <Bot className="w-8 h-8 text-blue-600" />
            </div>
            <div>
              <p className="text-lg font-medium text-gray-900">¿En qué te ayudo?</p>
              <p className="text-sm text-gray-500 max-w-sm mt-1">
                Prueba con "¿cuánto gasté en Uber en marzo?" o "recibos relacionados con viajes".
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-6 pb-4">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex gap-4 ${msg.role === "user" ? "flex-row-reverse" : ""}`}
              >
                <div
                  className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${msg.role === "user" ? "bg-gray-200" : "bg-blue-600"}`}
                >
                  {msg.role === "user" ? (
                    <User className="w-5 h-5 text-gray-600" />
                  ) : (
                    <Bot className="w-5 h-5 text-white" />
                  )}
                </div>
                <div
                  className={`flex flex-col gap-2 max-w-[80%] ${msg.role === "user" ? "items-end" : "items-start"}`}
                >
                  <div
                    className={`px-4 py-3 rounded-2xl ${msg.role === "user" ? "bg-gray-900 text-white rounded-tr-sm" : "bg-white border border-gray-200 shadow-sm rounded-tl-sm text-gray-800"}`}
                  >
                    <p className="whitespace-pre-wrap text-sm leading-relaxed">
                      {msg.content ||
                        (msg.role === "assistant" && <span className="animate-pulse">...</span>)}
                    </p>
                  </div>
                  {msg.citations && msg.citations.length > 0 && (
                    <div className="flex flex-wrap gap-2 mt-1">
                      {msg.citations.map((cit) => (
                        <CitationChip key={cit.expense_id} citation={cit} />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="p-4 bg-white border-t">
        <form onSubmit={handleSubmit} className="flex gap-4 max-w-4xl mx-auto relative">
          <Input
            className="flex-1 rounded-full pl-6 pr-14 h-12 bg-gray-50 border-gray-300 focus-visible:ring-blue-500"
            placeholder="Pregunta sobre tus gastos..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={isLoading}
          />
          <Button
            type="submit"
            size="icon"
            disabled={!input.trim() || isLoading}
            className="absolute right-1 top-1 h-10 w-10 rounded-full bg-blue-600 hover:bg-blue-700"
          >
            {isLoading ? (
              <Loader2 className="w-4 h-4 animate-spin text-white" />
            ) : (
              <Send className="w-4 h-4 text-white" />
            )}
          </Button>
        </form>
      </div>
    </div>
  );
}
