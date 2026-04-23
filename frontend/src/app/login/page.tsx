"use client";

import { useAuth } from "@/lib/auth/use-auth";
import { isDevAuth } from "@/lib/auth/dev-token";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { signInWithRedirect } from "aws-amplify/auth";

export default function LoginPage() {
  const { user, loading } = useAuth();

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

  const handleLogin = async () => {
    try {
      if (isDevAuth()) {
        // Dev-mode: useAuth already mints the token on mount. Just go home.
        window.location.href = "/";
        return;
      }
      await signInWithRedirect();
    } catch (error) {
      console.error("Error signing in", error);
    }
  };

  const devMode = isDevAuth();

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50 p-4">
      <Card className="w-full max-w-md shadow-lg border-0 ring-1 ring-gray-200">
        <CardHeader className="text-center pb-6">
          <CardTitle className="text-3xl font-bold tracking-tight text-gray-900">Nexus</CardTitle>
          <CardDescription className="text-base text-gray-500">
            Inicia sesión para auditar tus recibos
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            className="w-full font-medium h-12"
            onClick={handleLogin}
          >
            {devMode ? "Entrar (modo dev)" : "Iniciar sesión con Cognito"}
          </Button>
          {devMode && (
            <p className="mt-6 text-center text-xs text-gray-400">
              Modo dev activo — el backend emite un JWT HS256 firmado con DEV_JWT_SECRET.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
