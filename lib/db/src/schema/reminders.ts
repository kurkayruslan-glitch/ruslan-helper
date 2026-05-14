import { sql } from "drizzle-orm";
import { pgTable, text, bigint, boolean, integer, timestamp, index } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const remindersTable = pgTable(
  "reminders",
  {
    id: text("id").primaryKey(),
    chatId: bigint("chat_id", { mode: "number" }).notNull(),
    text: text("text").notNull(),
    fireAt: timestamp("fire_at").notNull(),
    createdAt: timestamp("created_at").notNull().defaultNow(),
    fired: boolean("fired").notNull().default(false),
    failures: integer("failures").notNull().default(0),
  },
  (t) => [
    index("reminders_due_idx")
      .on(t.fireAt)
      .where(sql`${t.fired} = false`),
  ],
);

export const insertReminderSchema = createInsertSchema(remindersTable);
export type InsertReminder = z.infer<typeof insertReminderSchema>;
export type Reminder = typeof remindersTable.$inferSelect;
