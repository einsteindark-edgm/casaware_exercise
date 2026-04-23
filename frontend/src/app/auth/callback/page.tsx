"use client";

import { useEffect, useState } from "react";
import { Hub } from "aws-amplify/utils";
import { fetchAuthSession } from "aws-amplify/auth";
import "@/lib/auth/amplify-config";

export default function AuthCallbackPage() {
  const [message, setMessage] = useState("Finishing sign in...");

  useEffect(() => {
    let cancelled = false;

    const finish = async () => {
      try {
        const session = await fetchAuthSession();
        if (!cancelled && session.tokens?.idToken) {
          window.location.replace("/");
        }
      } catch {
        // Amplify may still be exchanging the code; wait for the Hub event.
      }
    };

    const unsub = Hub.listen("auth", ({ payload }) => {
      if (payload.event === "signedIn") {
        window.location.replace("/");
      }
      if (payload.event === "signInWithRedirect_failure") {
        setMessage("Sign in failed. Redirecting to login.");
        setTimeout(() => window.location.replace("/login"), 1500);
      }
    });

    finish();

    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50">
      <div className="flex flex-col items-center gap-4">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900" />
        <p className="text-sm text-gray-600">{message}</p>
      </div>
    </div>
  );
}
