import { z } from "zod";

export const EventTypeSchema = z.enum([
  "workflow.started",
  "workflow.ocr_progress",
  "workflow.hitl_required",
  "workflow.hitl_resolved",
  "workflow.completed",
  "workflow.failed",
  "chat.token",
  "chat.complete",
  "ping",
]);

export const EventEnvelopeSchema = z.object({
  schema_version: z.literal("1.0"),
  event_id: z.string(),
  event_type: EventTypeSchema,
  workflow_id: z.string().nullish(),
  tenant_id: z.string(),
  user_id: z.string().nullish(),
  expense_id: z.string().nullish(),
  timestamp: z.string().datetime(),
  payload: z.record(z.string(), z.unknown()),
});

export type EventEnvelope = z.infer<typeof EventEnvelopeSchema>;

export const HITLRequiredPayloadSchema = z.object({
  hitl_task_id: z.string(),
  fields_in_conflict: z.array(z.object({
    field: z.string(),
    user_value: z.unknown(),
    ocr_value: z.unknown(),
    confidence: z.number(),
  })),
});

export type HITLRequiredPayload = z.infer<typeof HITLRequiredPayloadSchema>;
