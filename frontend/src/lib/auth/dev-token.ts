"use client";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const STORAGE_KEY = "nexus:dev:id_token";

interface DevToken {
  id_token: string;
  sub: string;
  tenant_id: string;
  email: string;
  expires_at: number;
}

let inflight: Promise<DevToken> | null = null;

function decodeExpiry(jwt: string): number {
  try {
    const [, payload] = jwt.split(".");
    const json = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return (json.exp ?? 0) * 1000;
  } catch {
    return 0;
  }
}

export function isDevAuth(): boolean {
  if (typeof window === "undefined") return false;
  if (window.location.search.includes("fakeAuth=true")) return true;
  return process.env.NEXT_PUBLIC_AUTH_MODE === "dev";
}

export async function getDevToken(): Promise<DevToken> {
  if (typeof window === "undefined") {
    throw new Error("getDevToken is client-only");
  }
  const cached = window.localStorage.getItem(STORAGE_KEY);
  if (cached) {
    try {
      const parsed = JSON.parse(cached) as DevToken;
      if (parsed.expires_at - Date.now() > 60_000) {
        return parsed;
      }
    } catch {
      /* fallthrough to refetch */
    }
  }

  if (!inflight) {
    inflight = (async () => {
      const res = await fetch(`${API_URL}/api/v1/dev/token`, {
        method: "GET",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        throw new Error(`dev token mint failed: ${res.status}`);
      }
      const data = await res.json();
      const token: DevToken = {
        id_token: data.id_token,
        sub: data.sub,
        tenant_id: data.tenant_id,
        email: data.email,
        expires_at: decodeExpiry(data.id_token),
      };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(token));
      return token;
    })().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

export function clearDevToken(): void {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}
