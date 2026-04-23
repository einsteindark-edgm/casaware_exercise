"use client";

import { useParams, useRouter } from "next/navigation";
import { useState, useEffect } from "react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  
  // Custom values
  const [customValues, setCustomValues] = useState<Record<string, string>>({});

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
      .catch((err) => {
        console.error("Failed to load HITL task", err);
        toast.error("No se pudo cargar la tarea");
      })
      .finally(() => setLoading(false));
  }, [taskId, pendingHITL]);

  const handleResolve = async (decision: "accept_ocr" | "keep_user_value" | "custom") => {
    setSubmitting(true);
    try {
      await apiClient.post(`api/v1/hitl/${taskId}/resolve`, {
        json: {
          decision,
          resolved_fields: decision === "custom" ? customValues : {},
        },
      });
      toast.success("Tarea resuelta con éxito");
      removePendingHITL(taskId);
      router.push(`/expenses/${task.expenseId}`);
    } catch (err) {
      console.error(err);
      toast.error("No se pudo resolver la tarea");
      setSubmitting(false);
    }
  };

  if (loading) return <div>Cargando tarea...</div>;
  if (!task) return <div>Tarea no encontrada</div>;

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Revisión Manual Requerida</h1>
        <p className="text-gray-500">El auditor automático detectó discrepancias. Por favor, resuelve el conflicto.</p>
      </div>

      <div className="space-y-6">
        {(task.fieldsInConflict || []).map((conflict: ConflictField) => (
          <Card key={conflict.field} className="border-orange-200">
            <CardHeader className="bg-orange-50/50 pb-4">
              <CardTitle className="text-lg flex items-center gap-2">
                <AlertTriangle className="w-5 h-5 text-orange-500" />
                Campo: <span className="uppercase text-orange-900">{conflict.field}</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-6">
              <div className="grid grid-cols-2 gap-8">
                {/* User Value */}
                <div className="space-y-2 p-4 rounded-lg bg-gray-50 border border-gray-100">
                  <Label className="text-gray-500 uppercase text-xs font-bold tracking-wider">Tú Reportaste</Label>
                  <p className="text-2xl font-medium">{String(conflict.user_value)}</p>
                </div>
                
                {/* OCR Value */}
                <div className="space-y-2 p-4 rounded-lg bg-blue-50 border border-blue-100">
                  <div className="flex justify-between items-center">
                    <Label className="text-blue-600 uppercase text-xs font-bold tracking-wider">OCR Detectó</Label>
                    <Badge variant="outline" className="text-blue-700 bg-blue-100 border-blue-200">
                      Conf: {conflict.confidence}%
                    </Badge>
                  </div>
                  <p className="text-2xl font-medium text-blue-900">{String(conflict.ocr_value)}</p>
                </div>
              </div>

              <div className="mt-6">
                <Label>Edición Manual (Opcional)</Label>
                <Input 
                  className="mt-2"
                  placeholder="Escribe el valor correcto si ambos están mal..."
                  value={customValues[conflict.field] || ""}
                  onChange={(e) => setCustomValues({...customValues, [conflict.field]: e.target.value})}
                />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="flex justify-end gap-4 mt-8 pt-4 border-t border-gray-200">
        <Button 
          variant="outline" 
          onClick={() => handleResolve("keep_user_value")}
          disabled={submitting}
        >
          Mantener mi valor
        </Button>
        <Button 
          variant="default"
          onClick={() => handleResolve(Object.keys(customValues).length > 0 ? "custom" : "accept_ocr")}
          disabled={submitting}
          className="bg-blue-600 hover:bg-blue-700"
        >
          <Check className="w-4 h-4 mr-2" />
          {Object.keys(customValues).length > 0 ? "Aceptar valor editado" : "Aceptar sugerencia OCR"}
        </Button>
      </div>
    </div>
  );
}
