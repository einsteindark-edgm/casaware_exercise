"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { apiClient } from "@/lib/api/client";

const formSchema = z.object({
  amount: z.string().min(1, "El monto es requerido"),
  currency: z.string().min(1, "La moneda es requerida"),
  date: z.string().min(1, "La fecha es requerida"),
  vendor: z.string().min(1, "El proveedor es requerido"),
  category: z.string().min(1, "La categoría es requerida"),
});

export default function NewExpensePage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [isUploading, setIsUploading] = useState(false);

  const { register, handleSubmit, formState: { errors } } = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      currency: "COP"
    }
  });

  const onSubmit = async (values: z.infer<typeof formSchema>) => {
    if (!file) {
      toast.error("Por favor selecciona un recibo (PDF o Imagen)");
      return;
    }

    setIsUploading(true);
    setUploadProgress(0);

    const formData = new FormData();
    // Backend contract (POST /api/v1/expenses): multipart with `file` + `expense_json`.
    formData.append("file", file);
    formData.append(
      "expense_json",
      JSON.stringify({
        amount: Number(values.amount),
        currency: values.currency,
        date: values.date,
        vendor: values.vendor,
        category: values.category,
      })
    );

    try {
      const progressInterval = setInterval(() => {
        setUploadProgress(p => Math.min(p + 15, 95));
      }, 200);

      const response = await apiClient.post("api/v1/expenses", {
        body: formData,
        timeout: 60_000,
      }).json<{ expense_id: string; workflow_id: string; status: string }>();

      clearInterval(progressInterval);
      setUploadProgress(100);
      
      toast.success("Recibo subido exitosamente");
      // Redirigir al detalle para ver el timeline en vivo
      router.push(`/expenses/${response.expense_id}`);
      
    } catch (error) {
      console.error(error);
      toast.error("Error al subir el recibo");
      setIsUploading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Reportar Gasto</h1>
        <p className="text-gray-500">Sube tu recibo e ingresa los datos para iniciar la auditoría.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Detalles del Recibo</CardTitle>
          <CardDescription>
            Los datos reportados aquí serán validados contra el recibo adjunto por nuestro agente de auditoría.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            
            <div className="space-y-2">
              <Label htmlFor="receipt">Recibo (PDF o Imagen)</Label>
              <Input 
                id="receipt" 
                type="file" 
                accept="application/pdf,image/*"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
                disabled={isUploading}
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="amount">Monto reportado</Label>
                <Input id="amount" type="number" step="0.01" {...register("amount")} disabled={isUploading} />
                {errors.amount && <p className="text-xs text-red-500">{errors.amount.message}</p>}
              </div>
              <div className="space-y-2">
                <Label htmlFor="currency">Moneda</Label>
                <Input id="currency" {...register("currency")} disabled={isUploading} />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="date">Fecha</Label>
              <Input id="date" type="date" {...register("date")} disabled={isUploading} />
              {errors.date && <p className="text-xs text-red-500">{errors.date.message}</p>}
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="vendor">Proveedor</Label>
                <Input id="vendor" {...register("vendor")} disabled={isUploading} />
                {errors.vendor && <p className="text-xs text-red-500">{errors.vendor.message}</p>}
              </div>
              <div className="space-y-2">
                <Label htmlFor="category">Categoría</Label>
                <Input id="category" {...register("category")} disabled={isUploading} />
                {errors.category && <p className="text-xs text-red-500">{errors.category.message}</p>}
              </div>
            </div>

            {isUploading && (
              <div className="space-y-2">
                <div className="flex justify-between text-xs text-gray-500">
                  <span>Subiendo e iniciando auditoría...</span>
                  <span>{uploadProgress}%</span>
                </div>
                <div className="h-2 w-full bg-gray-100 rounded-full overflow-hidden">
                  <div 
                    className="h-full bg-blue-600 transition-all duration-300" 
                    style={{ width: `${uploadProgress}%` }}
                  />
                </div>
              </div>
            )}

            <Button type="submit" className="w-full" disabled={isUploading}>
              {isUploading ? 'Procesando...' : 'Subir y Auditar'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
