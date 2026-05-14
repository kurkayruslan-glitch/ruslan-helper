import { pgTable, text, serial, timestamp } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const memoryFactsTable = pgTable("memory_facts", {
  id: serial("id").primaryKey(),
  fact: text("fact").notNull().unique(),
  addedAt: timestamp("added_at").notNull().defaultNow(),
});

export const insertMemoryFactSchema = createInsertSchema(memoryFactsTable).omit({
  id: true,
});
export type InsertMemoryFact = z.infer<typeof insertMemoryFactSchema>;
export type MemoryFact = typeof memoryFactsTable.$inferSelect;
