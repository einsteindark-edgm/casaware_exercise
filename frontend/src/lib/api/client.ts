import ky from "ky";
import { fetchAuthSession } from "aws-amplify/auth";
import { getDevToken, isDevAuth } from "@/lib/auth/dev-token";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function getBearerToken(): Promise<string> {
  if (isDevAuth()) {
    const token = await getDevToken();
    return token.id_token;
  }
  const session = await fetchAuthSession();
  return session.tokens?.idToken?.toString() ?? "";
}

export const apiClient = ky.create({
  prefix: API_URL,
  hooks: {
    beforeRequest: [
      async ({ request }) => {
        try {
          const token = await getBearerToken();
          if (token) {
            request.headers.set("Authorization", `Bearer ${token}`);
          }
        } catch (err) {
          console.warn("No auth session available for API request", err);
        }
      },
    ],
  },
});
