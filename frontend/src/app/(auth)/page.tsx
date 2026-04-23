"use client";

import { useSSEStore } from "@/stores/sse-store";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import Link from "next/link";
import { AlertCircle, ArrowRight, FileText } from "lucide-react";

export default function DashboardPage() {
  const pendingHITL = useSSEStore((state) => state.pendingHITL);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Dashboard</h1>
        <p className="text-gray-500">General overview of your activity and audits.</p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        {/* Simulated KPI Cards */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Total Spent</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">$1,250.00</div>
            <p className="text-xs text-muted-foreground">Current month</p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Audited Receipts</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">+12</div>
            <p className="text-xs text-muted-foreground">Current month</p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {/* HITL Widget */}
        <Card className="col-span-1 border-orange-200 bg-orange-50/30">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-orange-800">
              <AlertCircle className="h-5 w-5" />
              Pending your review
            </CardTitle>
            <CardDescription className="text-orange-600/80">
              Audits stopped due to discrepancies detected by OCR.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {pendingHITL.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">No pending tasks. Everything is in order.</p>
            ) : (
              <div className="space-y-3">
                {pendingHITL.map((task) => (
                  <div key={task.taskId} className="flex items-center justify-between bg-white p-3 rounded-md border border-orange-100 shadow-sm">
                    <div>
                      <p className="text-sm font-medium text-gray-900">Expense: {task.expenseId.split('-')[0]}...</p>
                      <p className="text-xs text-gray-500">{task.fieldsInConflict.length} fields in conflict</p>
                    </div>
                    <Link href={`/hitl/${task.taskId}`} className={buttonVariants({ size: "sm", variant: "outline", className: "border-orange-200 text-orange-700 hover:bg-orange-50" })}>
                      Resolve
                    </Link>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Quick Actions */}
        <Card className="col-span-1">
          <CardHeader>
            <CardTitle>Quick Actions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Link href="/expenses/new" className={buttonVariants({ className: "w-full justify-start" })}>
              <FileText className="mr-2 h-4 w-4" />
              Upload new receipt
            </Link>
            <Link href="/expenses" className={buttonVariants({ variant: "outline", className: "w-full justify-start" })}>
              <ArrowRight className="mr-2 h-4 w-4" />
              View all expenses
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
