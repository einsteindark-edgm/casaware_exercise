"use client";

import { useParams, useRouter } from "next/navigation";
import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { apiClient } from "@/lib/api/client";
import { toast } from "sonner";
import { useSSEStore } from "@/stores/sse-store";
import { AlertTriangle, Check } from "lucide-react";

interface ConflictField {
  field: string;
  user_value: any;
  ocr_value: any;
  confidence: number;
}

export default function HITLResolverPage() {
  const params = useParams();
  const router = useRouter();
  const taskId = params.taskId as string;
  const pendingHITL = useSSEStore(state => state.pendingHITL);
  const removePendingHITL = useSSEStore(state => state.removePendingHITL);

  const [task, setTask] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    // Prefer the in-memory SSE cache (no round-trip) so we don't flash loading
    // state when the user clicks a toast right after the hitl_required event.
    const storeTask = pendingHITL.find(t => t.taskId === taskId);
    if (storeTask) {
      setTask(storeTask);
      setLoading(false);
      return;
    }
    apiClient.get(`api/v1/hitl/${taskId}`).json<{
        task_id: string;
        expense_id: string | null;
        workflow_id: string;
        status: string;
        fields_in_conflict: ConflictField[];
      }>()
      .then((data) => {
        setTask({
          taskId: data.task_id,
          expenseId: data.expense_id,
          workflowId: data.workflow_id,
          status: data.status,
          fieldsInConflict: data.fields_in_conflict || [],
        });
      })
      .catch(() => {
        toast.error("Could not load task");
      })
      .finally(() => setLoading(false));
  }, [taskId, pendingHITL]);

  // UX estricta (abril 2026): el único decision válido es accept_ocr y
  // resolved_fields siempre vacío. El backend además forzará esto por
  // defensa en profundidad.
  const handleAccept = async () => {
    setSubmitting(true);
    try {
      await apiClient.post(`api/v1/hitl/${taskId}/resolve`, {
        json: { decision: "accept_ocr", resolved_fields: {} },
      });
      toast.success("Correction accepted");
      removePendingHITL(taskId);
      router.push(`/expenses/${task.expenseId}`);
    } catch (err) {
      console.error(err);
      toast.error("Could not resolve task");
      setSubmitting(false);
    }
  };

  if (loading) return <div>Loading task...</div>;
  if (!task) return <div>Task not found</div>;

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Manual Review Required</h1>
        <p className="text-gray-500">
          The validator detected the corrections below. Accept them to keep the audit trail consistent —
          your original values stay preserved in history.
        </p>
      </div>

      <div className="space-y-6">
        {(task.fieldsInConflict || []).map((conflict: ConflictField) => (
          <Card key={conflict.field} className="border-orange-200">
            <CardHeader className="bg-orange-50/50 pb-4">
              <CardTitle className="text-lg flex items-center gap-2">
                <AlertTriangle className="w-5 h-5 text-orange-500" />
                Field: <span className="uppercase text-orange-900">{conflict.field}</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-6">
              <div className="grid grid-cols-2 gap-8">
                {/* User Value (preserved as historical record) */}
                <div className="space-y-2 p-4 rounded-lg bg-gray-50 border border-gray-100">
                  <Label className="text-gray-500 uppercase text-xs font-bold tracking-wider">You Reported</Label>
                  <p className="text-2xl font-medium">{String(conflict.user_value)}</p>
                  <p className="text-[11px] text-gray-400">Will stay in your history</p>
                </div>

                {/* OCR Value (will become the final value) */}
                <div className="space-y-2 p-4 rounded-lg bg-blue-50 border border-blue-100">
                  <div className="flex justify-between items-center">
                    <Label className="text-blue-600 uppercase text-xs font-bold tracking-wider">Suggested correction</Label>
                    <Badge variant="outline" className="text-blue-700 bg-blue-100 border-blue-200">
                      Conf: {conflict.confidence}%
                    </Badge>
                  </div>
                  <p className="text-2xl font-medium text-blue-900">{String(conflict.ocr_value)}</p>
                  <p className="text-[11px] text-blue-600">Will become the final value</p>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="flex justify-end mt-8 pt-4 border-t border-gray-200">
        <Button
          variant="default"
          onClick={handleAccept}
          disabled={submitting}
          className="bg-blue-600 hover:bg-blue-700"
        >
          <Check className="w-4 h-4 mr-2" />
          Accept correction
        </Button>
      </div>
    </div>
  );
}
