"use client";

import { Sidebar } from "@/components/sidebar/sidebar";
import { ChatArea } from "@/components/chat/chat-area";

export default function Home() {
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      <Sidebar />
      <ChatArea />
    </div>
  );
}
