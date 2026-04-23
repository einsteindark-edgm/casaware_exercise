import { Shell } from "@/components/shell/shell";

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <Shell>{children}</Shell>;
}
