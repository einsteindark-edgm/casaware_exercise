"use client";

import { useState, useRef, useEffect } from "react";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { apiClient, getBearerToken } from "@/lib/api/client";
import { subscribeToEvents } from "@/lib/sse/event-source";
import { useAuth } from "@/lib/auth/use-auth";
import { User, Bot, Send, Loader2 } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: any[];
}

export default function ChatPage() {
  const { user } = useAuth();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage: ChatMessage = { id: Date.now().toString(), role: "user", content: input };
    setMessages(prev => [...prev, userMessage]);
    setInput("");
    setIsLoading(true);

    const asstMsgId = (Date.now() + 1).toString();
    setMessages(prev => [...prev, { id: asstMsgId, role: "assistant", content: "" }]);

    try {
      // 1. Post to chat endpoint
      const response: any = await apiClient.post("api/v1/chat", {
        json: { message: userMessage.content, session_id: sessionId }
      }).json();

      if (response.session_id) setSessionId(response.session_id);

      const workflowId = response.workflow_id;

      // 2. Open SSE against the dedicated chat stream endpoint.
      const sub = subscribeToEvents({
        url: `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/v1/chat/stream/${workflowId}`,
        getToken: getBearerToken,
        onEvent: (event) => {
          if (event.event_type === "chat.token") {
            const token = (event.payload as any).token || "";
            setMessages(prev => prev.map(msg => 
              msg.id === asstMsgId ? { ...msg, content: msg.content + token } : msg
            ));
          } else if (event.event_type === "chat.complete") {
            const citations = (event.payload as any).citations;
            setMessages(prev => prev.map(msg => 
              msg.id === asstMsgId ? { ...msg, citations } : msg
            ));
            setIsLoading(false);
            sub.close();
          }
        },
        onError: (err) => {
          console.error("Chat SSE error", err);
          setIsLoading(false);
        }
      });
      
    } catch (err) {
      console.error(err);
      // FAKE RESPONSE FALLBACK FOR LOCAL DEV without backend
      let fakeContent = "Esta es una respuesta simulada por falta de conexión al backend. Te sugiero que revises tu recibo de Starbucks.";
      let idx = 0;
      const interval = setInterval(() => {
        setMessages(prev => prev.map(msg => 
          msg.id === asstMsgId ? { ...msg, content: msg.content + fakeContent[idx] } : msg
        ));
        idx++;
        if (idx >= fakeContent.length - 1) {
          clearInterval(interval);
          setMessages(prev => prev.map(msg => 
            msg.id === asstMsgId ? { ...msg, citations: [{ expense_id: "exp_123", snippet: "Starbucks $15.50" }] } : msg
          ));
          setIsLoading(false);
        }
      }, 50);
    }
  };

  return (
    <div className="h-[calc(100vh-120px)] max-h-[800px] flex flex-col mx-auto max-w-4xl border border-gray-200 rounded-lg bg-white shadow-sm overflow-hidden">
      <div className="bg-white border-b px-6 py-4">
        <h1 className="text-xl font-bold tracking-tight text-gray-900">Asistente de Auditoría RAG</h1>
        <p className="text-sm text-gray-500">Consulta tu historial de gastos usando lenguaje natural.</p>
      </div>

      <div className="flex-1 overflow-y-auto p-6 bg-gray-50" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center space-y-4">
            <div className="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center">
              <Bot className="w-8 h-8 text-blue-600" />
            </div>
            <div>
              <p className="text-lg font-medium text-gray-900">¿En qué te puedo ayudar hoy?</p>
              <p className="text-sm text-gray-500 max-w-sm mt-1">Prueba preguntando "¿Cuánto gasté en Starbucks el mes pasado?" o "¿Tengo políticas de viaje incumplidas?"</p>
            </div>
          </div>
        ) : (
          <div className="space-y-6 pb-4">
            {messages.map((msg) => (
              <div key={msg.id} className={`flex gap-4 ${msg.role === "user" ? "flex-row-reverse" : ""}`}>
                <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${msg.role === "user" ? "bg-gray-200" : "bg-blue-600"}`}>
                  {msg.role === "user" ? <User className="w-5 h-5 text-gray-600" /> : <Bot className="w-5 h-5 text-white" />}
                </div>
                <div className={`flex flex-col gap-2 max-w-[80%] ${msg.role === "user" ? "items-end" : "items-start"}`}>
                  <div className={`px-4 py-3 rounded-2xl ${msg.role === "user" ? "bg-gray-900 text-white rounded-tr-sm" : "bg-white border border-gray-200 shadow-sm rounded-tl-sm text-gray-800"}`}>
                    <p className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content || (msg.role === "assistant" && <span className="animate-pulse">...</span>)}</p>
                  </div>
                  
                  {/* Citations */}
                  {msg.citations && msg.citations.length > 0 && (
                    <div className="flex flex-wrap gap-2 mt-1">
                      {msg.citations.map((cit, i) => (
                        <Badge key={i} variant="secondary" className="text-xs bg-white border cursor-pointer hover:bg-gray-50">
                          <Link href={`/expenses/${cit.expense_id}`}>
                            Referencia [{i+1}]: {cit.snippet.substring(0, 20)}...
                          </Link>
                        </Badge>
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
            {isLoading ? <Loader2 className="w-4 h-4 animate-spin text-white" /> : <Send className="w-4 h-4 text-white" />}
          </Button>
        </form>
      </div>
    </div>
  );
}
