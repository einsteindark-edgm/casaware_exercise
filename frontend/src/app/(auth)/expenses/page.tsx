"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { buttonVariants } from "@/components/ui/button";
import Link from "next/link";
import { FileText, ArrowRight, Clock, CheckCircle, AlertTriangle, XCircle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api/client";

interface ExpenseItem {
  expense_id: string;
  vendor: string;
  amount: number;
  currency: string;
  date: string;
  status: "pending" | "processing" | "hitl_required" | "approved" | "rejected";
}

interface ExpenseListResponse {
  items: ExpenseItem[];
  next_cursor: string | null;
}

export default function ExpensesListPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["expenses"],
    queryFn: () =>
      apiClient.get("api/v1/expenses", { searchParams: { limit: 50 } }).json<ExpenseListResponse>(),
    refetchInterval: 15_000,
  });

  const items = data?.items ?? [];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">My Receipts</h1>
          <p className="text-gray-500">History of reported expenses.</p>
        </div>
        <Link href="/expenses/new" className={buttonVariants()}>
          <FileText className="mr-2 h-4 w-4" />
          Report Expense
        </Link>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent Expenses</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="py-8 text-center text-sm text-gray-500">Loading expenses…</p>
          ) : isError ? (
            <p className="py-8 text-center text-sm text-red-500">Error loading expenses.</p>
          ) : items.length === 0 ? (
            <p className="py-8 text-center text-sm text-gray-500">
              You haven't reported any expenses yet. Start by uploading a receipt.
            </p>
          ) : (
            <div className="divide-y divide-gray-100">
              {items.map(exp => (
                <div key={exp.expense_id} className="py-4 flex items-center justify-between">
                  <div className="flex flex-col">
                    <span className="font-medium text-gray-900">{exp.vendor}</span>
                    <span className="text-sm text-gray-500">{new Date(exp.date).toLocaleDateString()}</span>
                  </div>
                  <div className="flex items-center gap-6">
                    <div className="text-right">
                      <span className="font-bold text-gray-900">${exp.amount.toFixed(2)}</span>
                      <span className="text-xs text-gray-500 ml-1">{exp.currency}</span>
                    </div>
                    <div className="w-32">
                      {exp.status === 'approved' && <span className="flex items-center text-xs text-green-600"><CheckCircle className="w-3 h-3 mr-1" /> Approved</span>}
                      {(exp.status === 'pending' || exp.status === 'processing') && <span className="flex items-center text-xs text-blue-600"><Clock className="w-3 h-3 mr-1" /> Processing</span>}
                      {exp.status === 'hitl_required' && <span className="flex items-center text-xs text-orange-600"><AlertTriangle className="w-3 h-3 mr-1" /> Action required</span>}
                      {exp.status === 'rejected' && <span className="flex items-center text-xs text-red-600"><XCircle className="w-3 h-3 mr-1" /> Rejected</span>}
                    </div>
                    <Link href={`/expenses/${exp.expense_id}`} className={buttonVariants({ variant: "ghost", size: "sm" })}>
                      View <ArrowRight className="ml-1 w-4 h-4" />
                    </Link>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
