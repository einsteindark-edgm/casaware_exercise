"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth/use-auth";
import { isDevAuth } from "@/lib/auth/dev-token";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { signIn } from "aws-amplify/auth";
import "@/lib/auth/amplify-config";

export default function LoginPage() {
  const { user, loading } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900"></div>
      </div>
    );
  }

  if (user) {
    window.location.href = "/";
    return null;
  }

  const devMode = isDevAuth();

  const handleDevLogin = () => {
    window.location.href = "/";
  };

  const handleCognitoLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const { isSignedIn, nextStep } = await signIn({
        username: email,
        password,
        options: { authFlowType: "USER_SRP_AUTH" },
      });
      if (isSignedIn) {
        window.location.href = "/";
        return;
      }
      setError(`Extra step required: ${nextStep.signInStep}`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Sign in failed";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50 p-4">
      <Card className="w-full max-w-md shadow-lg border-0 ring-1 ring-gray-200">
        <CardHeader className="text-center pb-6">
          <CardTitle className="text-3xl font-bold tracking-tight text-gray-900">Nexus</CardTitle>
          <CardDescription className="text-base text-gray-500">
            Log in to audit your receipts
          </CardDescription>
        </CardHeader>
        <CardContent>
          {devMode ? (
            <Button className="w-full font-medium h-12" onClick={handleDevLogin}>
              Log in (dev mode)
            </Button>
          ) : (
            <form onSubmit={handleCognitoLogin} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  disabled={submitting}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={submitting}
                />
              </div>
              {error && (
                <p className="text-sm text-red-600" role="alert">
                  {error}
                </p>
              )}
              <Button type="submit" className="w-full font-medium h-12" disabled={submitting}>
                {submitting ? "Signing in..." : "Log in"}
              </Button>
            </form>
          )}
          {devMode && (
            <p className="mt-6 text-center text-xs text-gray-400">
              Dev mode active — backend issues an HS256 JWT signed with DEV_JWT_SECRET.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
