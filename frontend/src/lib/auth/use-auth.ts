"use client";

import { useEffect, useState } from "react";
import { fetchAuthSession, signOut as amplifySignOut } from "aws-amplify/auth";
import "./amplify-config"; // Ensure it's configured
import { clearDevToken, getDevToken, isDevAuth } from "./dev-token";

export interface User {
  sub: string;
  email: string;
  tenantId: string;
  role: string;
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (isDevAuth()) {
      getDevToken()
        .then((token) => {
          setUser({
            sub: token.sub,
            email: token.email,
            tenantId: token.tenant_id,
            role: "user",
          });
        })
        .catch((err) => {
          console.error("Dev token fetch failed:", err);
          setUser(null);
        })
        .finally(() => setLoading(false));
      return;
    }

    fetchAuthSession()
      .then((session) => {
        const idToken = session.tokens?.idToken;
        if (idToken) {
          setUser({
            sub: idToken.payload.sub as string,
            email: idToken.payload.email as string,
            tenantId: idToken.payload["custom:tenant_id"] as string,
            role: idToken.payload["custom:role"] as string,
          });
        }
      })
      .catch((err) => {
        console.error("Auth session error:", err);
        setUser(null);
      })
      .finally(() => setLoading(false));
  }, []);

  const signOut = async () => {
    try {
      if (isDevAuth()) {
        clearDevToken();
        setUser(null);
        window.location.href = "/login";
        return;
      }
      await amplifySignOut();
      setUser(null);
      window.location.href = "/login";
    } catch (err) {
      console.error("Error signing out:", err);
    }
  };

  return { user, loading, signOut };
}
