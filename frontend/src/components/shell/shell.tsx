"use client";

import { useAuth } from "@/lib/auth/use-auth";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { LogOut, LayoutDashboard, Receipt, FileText, MessageSquare } from "lucide-react";
import { useSSEEvents } from "@/lib/sse/use-sse-events";
import { useSSEStore } from "@/stores/sse-store";
import { toast } from "sonner";

export function Shell({ children }: { children: React.ReactNode }) {
  const { user, loading, signOut } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  
  // Establecer conexión global a SSE
  const { connected } = useSSEEvents({ enabled: !!user });
  const pendingHITL = useSSEStore((state) => state.pendingHITL);

  useEffect(() => {
    if (!loading && !user && pathname !== "/login") {
      router.replace("/login");
    }
  }, [user, loading, router, pathname]);

  // Toast listener global para nuevos HITL
  useEffect(() => {
    // Si la cantidad de pendingHITL cambia, podríamos mostrar un toast
    // Para simplificar, confiaremos en que el estado se reflejará en la UI.
  }, [pendingHITL.length]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900"></div>
      </div>
    );
  }

  if (!user) {
    return null; // El redirect lo maneja el useEffect
  }

  const navItems = [
    { name: "Dashboard", href: "/", icon: LayoutDashboard },
    { name: "Recibos", href: "/expenses", icon: Receipt },
    { name: "Nuevo Gasto", href: "/expenses/new", icon: FileText },
    { name: "Chatbot RAG", href: "/chat", icon: MessageSquare },
  ];

  return (
    <div className="flex min-h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-64 flex-shrink-0 bg-white border-r border-gray-200 flex flex-col">
        <div className="h-16 flex items-center px-6 border-b border-gray-200">
          <span className="text-xl font-bold text-gray-900 tracking-tight">Nexus</span>
          {connected && (
            <span className="ml-2 flex h-2 w-2 relative">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500"></span>
            </span>
          )}
        </div>
        
        <nav className="flex-1 px-4 py-6 space-y-1">
          {navItems.map((item) => {
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.name}
                href={item.href}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors ${
                  isActive 
                    ? "bg-gray-100 text-gray-900" 
                    : "text-gray-600 hover:bg-gray-50 hover:text-gray-900"
                }`}
              >
                <item.icon className="w-5 h-5 opacity-70" />
                {item.name}
              </Link>
            );
          })}
        </nav>

        <div className="p-4 border-t border-gray-200">
          <div className="flex items-center justify-between mb-4 px-2">
            <div className="text-sm truncate">
              <p className="font-medium text-gray-900 truncate">{user.email}</p>
              <p className="text-xs text-gray-500 truncate">{user.role}</p>
            </div>
          </div>
          <Button 
            variant="outline" 
            className="w-full justify-start text-gray-600"
            onClick={signOut}
          >
            <LogOut className="w-4 h-4 mr-2" />
            Cerrar sesión
          </Button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
            {children}
          </div>
        </div>
      </main>
    </div>
  );
}
