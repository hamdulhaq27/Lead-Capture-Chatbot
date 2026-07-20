"use client";

import React, { useRef, useEffect } from "react";
import { Paperclip, Mic, Send, ArrowUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled?: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [input, setInput] = React.useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    if (input.trim() && !disabled) {
      onSend(input);
      setInput("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [input]);

  return (
    <div className="p-4 bg-background/80 backdrop-blur-sm sticky bottom-0 border-t">
      <div className="max-w-3xl mx-auto relative flex items-end gap-2 bg-muted/50 p-2 rounded-3xl border focus-within:ring-1 focus-within:ring-indigo-500 transition-shadow">
        <Button variant="ghost" size="icon" className="shrink-0 rounded-full h-10 w-10 text-muted-foreground hover:text-foreground">
          <Paperclip className="w-5 h-5" />
        </Button>
        <div className="flex-1 max-h-[200px] overflow-y-auto custom-scrollbar relative py-2">
          <Textarea
            ref={textareaRef}
            placeholder="Ask anything about BrightReach..."
            className="min-h-[24px] max-h-[200px] resize-none border-0 bg-transparent p-0 focus-visible:ring-0 shadow-none overflow-hidden block"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            rows={1}
          />
        </div>
        <Button variant="ghost" size="icon" className="shrink-0 rounded-full h-10 w-10 text-muted-foreground hover:text-foreground">
          <Mic className="w-5 h-5" />
        </Button>
        <Button
          size="icon"
          className="shrink-0 rounded-full h-10 w-10 bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm transition-transform active:scale-95 disabled:opacity-50 disabled:active:scale-100"
          onClick={handleSend}
          disabled={!input.trim() || disabled}
        >
          <ArrowUp className="w-5 h-5" />
        </Button>
      </div>
      <div className="text-center mt-2">
        <span className="text-[10px] text-muted-foreground">BrightReach AI can make mistakes. Consider verifying important information.</span>
      </div>
    </div>
  );
}
