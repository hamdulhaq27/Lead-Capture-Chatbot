"use client";

import React, { useState } from "react";
import { Plus, Search, MessageSquare, MoreHorizontal, Settings, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { ThemeToggle } from "@/components/theme-toggle";

const DUMMY_CONVERSATIONS = [
  { id: "1", title: "Explain SEO Packages", time: "Today" },
  { id: "2", title: "Book Discovery Call", time: "Today" },
  { id: "3", title: "What services do you offer?", time: "Yesterday" },
  { id: "4", title: "Pricing Information", time: "Previous 7 Days" },
  { id: "5", title: "Generate Marketing Strategy", time: "Previous 7 Days" },
];

export function Sidebar() {
  const [search, setSearch] = useState("");

  return (
    <div className="w-[300px] h-screen border-r bg-background flex flex-col hidden md:flex">
      {/* Header */}
      <div className="p-4 flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold">
            B
          </div>
          <div className="flex flex-col">
            <span className="font-semibold text-sm">BrightReach</span>
            <span className="text-xs text-muted-foreground">Virtual Assistant</span>
          </div>
        </div>

        <Button className="w-full justify-start gap-2" variant="outline">
          <Plus className="w-4 h-4" />
          New Chat
        </Button>

        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search..."
            className="pl-8 bg-muted/50 border-none"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Conversation List */}
      <ScrollArea className="flex-1 px-3">
        <div className="flex flex-col gap-1 pb-4">
          <div className="text-xs font-medium text-muted-foreground px-2 py-2 mt-2">Today</div>
          {DUMMY_CONVERSATIONS.filter(c => c.time === "Today").map((c) => (
            <Button key={c.id} variant="ghost" className="justify-start gap-2 h-9 px-2 text-sm font-normal group relative">
              <MessageSquare className="w-4 h-4 text-muted-foreground" />
              <span className="truncate flex-1 text-left">{c.title}</span>
              <div className="hidden group-hover:flex absolute right-1 bg-background/80 px-1 items-center gap-1 backdrop-blur-sm">
                <MoreHorizontal className="w-4 h-4 text-muted-foreground hover:text-foreground" />
              </div>
            </Button>
          ))}
          
          <div className="text-xs font-medium text-muted-foreground px-2 py-2 mt-4">Yesterday</div>
          {DUMMY_CONVERSATIONS.filter(c => c.time === "Yesterday").map((c) => (
            <Button key={c.id} variant="ghost" className="justify-start gap-2 h-9 px-2 text-sm font-normal group relative">
              <MessageSquare className="w-4 h-4 text-muted-foreground" />
              <span className="truncate flex-1 text-left">{c.title}</span>
            </Button>
          ))}
        </div>
      </ScrollArea>

      {/* Footer */}
      <div className="p-4 border-t flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="icon" className="text-muted-foreground">
            <Settings className="w-5 h-5" />
          </Button>
          <ThemeToggle />
        </div>
        <div className="flex items-center gap-3 mt-2 px-2 py-2 hover:bg-muted/50 rounded-lg cursor-pointer transition-colors">
          <Avatar className="w-8 h-8">
            <AvatarFallback><User className="w-4 h-4" /></AvatarFallback>
          </Avatar>
          <div className="flex flex-col flex-1">
            <span className="text-sm font-medium">Guest User</span>
            <span className="text-xs text-muted-foreground">Free Plan</span>
          </div>
        </div>
      </div>
    </div>
  );
}
