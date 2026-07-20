"use client";

import React from "react";
import { Menu, MoreHorizontal, Settings, Trash2 } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";

export function ChatHeader() {
  return (
    <div className="h-14 border-b flex items-center justify-between px-4 sticky top-0 bg-background/95 backdrop-blur z-10">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" className="md:hidden">
          <Menu className="w-5 h-5" />
        </Button>
        <Avatar className="w-8 h-8 rounded-lg">
          <AvatarFallback className="bg-indigo-600 text-white font-bold rounded-lg">B</AvatarFallback>
        </Avatar>
        <div className="flex flex-col">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm">BrightReach AI</span>
            <Badge variant="outline" className="text-[10px] h-5 px-1.5 bg-green-500/10 text-green-600 border-green-200">Online</Badge>
          </div>
          <span className="text-xs text-muted-foreground flex items-center gap-1">
            Powered by Groq <span className="w-1 h-1 rounded-full bg-muted-foreground/50"></span> Llama 3.3 70B
          </span>
        </div>
      </div>
      
      <div>
        <DropdownMenu>
          <DropdownMenuTrigger className={buttonVariants({ variant: "ghost", size: "icon" })}>
            <MoreHorizontal className="w-5 h-5 text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem>
              <Settings className="w-4 h-4 mr-2" />
              Settings
            </DropdownMenuItem>
            <DropdownMenuItem className="text-red-600 focus:text-red-600">
              <Trash2 className="w-4 h-4 mr-2" />
              Clear Chat
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
