export type Role = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  updatedAt: Date;
}
