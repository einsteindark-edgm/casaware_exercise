import Link from "next/link";
import { Receipt } from "lucide-react";
import { Badge } from "@/components/ui/badge";

export interface Citation {
  expense_id: string;
  vendor?: string | null;
  amount?: number | null;
  currency?: string | null;
  date?: string | null;
  category?: string | null;
  link: string;
  source?: string | null;
}

function formatAmount(amount?: number | null, currency?: string | null): string {
  if (amount === null || amount === undefined) return "";
  const num = Number(amount);
  if (!Number.isFinite(num)) return "";
  const fmt = num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${fmt} ${currency}` : fmt;
}

export function CitationChip({ citation }: { citation: Citation }) {
  const pieces = [
    citation.vendor,
    formatAmount(citation.amount, citation.currency),
    citation.date,
  ].filter(Boolean);
  const label = pieces.length > 0 ? pieces.join(" · ") : citation.expense_id;

  return (
    <Link href={citation.link} prefetch={false}>
      <Badge
        variant="secondary"
        className="text-xs bg-white border cursor-pointer hover:bg-gray-50 gap-1.5"
        title={`Ver recibo ${citation.expense_id}${citation.source ? ` · ${citation.source}` : ""}`}
      >
        <Receipt className="w-3 h-3" />
        {label}
      </Badge>
    </Link>
  );
}
