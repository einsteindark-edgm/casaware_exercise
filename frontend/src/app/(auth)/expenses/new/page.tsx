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
  amount: z.string().min(1, "Amount is required"),
  currency: z.string().min(1, "Currency is required"),
  date: z.string().min(1, "Date is required"),
  vendor: z.string().min(1, "Vendor is required"),
  category: z.string().min(1, "Category is required"),
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
      toast.error("Please select a receipt (PDF or Image)");
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
      
      toast.success("Receipt uploaded successfully");
      // Redirect to detail to see live timeline
      router.push(`/expenses/${response.expense_id}`);
      
    } catch (error) {
      console.error(error);
      toast.error("Error uploading receipt");
      setIsUploading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Report Expense</h1>
        <p className="text-gray-500">Upload your receipt and enter the data to start the audit.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Receipt Details</CardTitle>
          <CardDescription>
            The reported data here will be validated against the attached receipt by our audit agent.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            
            <div className="space-y-2">
              <Label htmlFor="receipt">Receipt (PDF or Image)</Label>
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
                <Label htmlFor="amount">Reported amount</Label>
                <Input id="amount" type="number" step="0.01" {...register("amount")} disabled={isUploading} />
                {errors.amount && <p className="text-xs text-red-500">{errors.amount.message}</p>}
              </div>
              <div className="space-y-2">
                <Label htmlFor="currency">Currency</Label>
                <Input id="currency" {...register("currency")} disabled={isUploading} />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="date">Date</Label>
              <Input id="date" type="date" {...register("date")} disabled={isUploading} />
              {errors.date && <p className="text-xs text-red-500">{errors.date.message}</p>}
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="vendor">Vendor</Label>
                <Input id="vendor" {...register("vendor")} disabled={isUploading} />
                {errors.vendor && <p className="text-xs text-red-500">{errors.vendor.message}</p>}
              </div>
              <div className="space-y-2">
                <Label htmlFor="category">Category</Label>
                <Input id="category" {...register("category")} disabled={isUploading} />
                {errors.category && <p className="text-xs text-red-500">{errors.category.message}</p>}
              </div>
            </div>

            {isUploading && (
              <div className="space-y-2">
                <div className="flex justify-between text-xs text-gray-500">
                  <span>Uploading and starting audit...</span>
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
              {isUploading ? 'Processing...' : 'Upload and Audit'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
