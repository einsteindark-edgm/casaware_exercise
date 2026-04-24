import { Amplify } from "aws-amplify";

// Fail loudly if the CI build-args were missing. Without this check, an
// undefined userPoolId only surfaces as "Auth UserPool not configured"
// deep inside Amplify at login time. This has bitten us once already
// (GHA role was missing ssm:GetParameter → build baked empty strings).
const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_APP_CLIENT_ID;
const domain = process.env.NEXT_PUBLIC_COGNITO_DOMAIN;
const appUrl = process.env.NEXT_PUBLIC_APP_URL;

if (!userPoolId || !userPoolClientId || !domain || !appUrl) {
  throw new Error(
    "NEXT_PUBLIC_COGNITO_* / NEXT_PUBLIC_APP_URL missing at build time. " +
      "The GHA build-args were not passed correctly. " +
      `Got: userPoolId=${userPoolId ? "OK" : "MISSING"} ` +
      `clientId=${userPoolClientId ? "OK" : "MISSING"} ` +
      `domain=${domain ? "OK" : "MISSING"} ` +
      `appUrl=${appUrl ? "OK" : "MISSING"}.`
  );
}

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId,
      userPoolClientId,
      loginWith: {
        oauth: {
          domain,
          scopes: ["openid", "email", "profile"],
          redirectSignIn: [`${appUrl}/auth/callback`],
          redirectSignOut: [`${appUrl}/login`],
          responseType: "code",
        },
      },
    },
  },
});
