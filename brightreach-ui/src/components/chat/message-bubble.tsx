"use client";

import React from "react";
import { motion } from "framer-motion";
import { format } from "date-fns";
import { Copy, ThumbsUp, ThumbsDown, RotateCcw, Check } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { ChatMessage } from "@/types";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const [copied, setCopied] = React.useState(false);
  const [displayContent, setDisplayContent] = React.useState(
    message.isStreaming ? "" : message.content
  );

  React.useEffect(() => {
    if (!message.isStreaming) {
      setDisplayContent(message.content);
      return;
    }

    let i = 0;
    const interval = setInterval(() => {
      // Simulate fast token streaming by advancing 2-3 characters at a time
      i += Math.floor(Math.random() * 3) + 1;
      setDisplayContent(message.content.substring(0, i));
      
      if (i >= message.content.length) {
        clearInterval(interval);
      }
    }, 15);

    return () => clearInterval(interval);
  }, [message.content, message.isStreaming]);

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex gap-4 w-full py-6 ${isUser ? "" : ""}`}
    >
      {!isUser && (
        <Avatar className="w-8 h-8 rounded-lg shrink-0 mt-1 shadow-sm">
          <AvatarFallback className="bg-indigo-600 text-white font-bold rounded-lg">B</AvatarFallback>
        </Avatar>
      )}

      <div className={`flex flex-col gap-2 w-full max-w-3xl ${isUser ? "items-end ml-auto" : ""}`}>
        {isUser ? (
          <div className="bg-muted px-5 py-3.5 rounded-2xl rounded-tr-sm text-[15px] text-foreground max-w-[85%] shadow-sm">
            {message.content}
          </div>
        ) : (
          <div className="flex flex-col gap-2 w-full">
            <div className="text-[15px] leading-relaxed text-foreground prose prose-neutral dark:prose-invert max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {displayContent.replace(/<[^>]*>?/gm, '')}
              </ReactMarkdown>
            </div>
            
            {/* Action Bar */}
            <div className="flex items-center gap-1 mt-2 text-muted-foreground">
              <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleCopy} title="Copy">
                {copied ? <Check className="w-4 h-4 text-green-500" /> : <Copy className="w-4 h-4" />}
              </Button>
              <Button variant="ghost" size="icon" className="h-8 w-8" title="Good response">
                <ThumbsUp className="w-4 h-4" />
              </Button>
              <Button variant="ghost" size="icon" className="h-8 w-8" title="Bad response">
                <ThumbsDown className="w-4 h-4" />
              </Button>
              <Button variant="ghost" size="icon" className="h-8 w-8" title="Regenerate">
                <RotateCcw className="w-4 h-4" />
              </Button>
              <span className="text-xs ml-2 opacity-50">{format(message.timestamp, "h:mm a")}</span>
            </div>
          </div>
        )}
      </div>

      {isUser && (
        <Avatar className="w-8 h-8 rounded-lg shrink-0 mt-1 shadow-sm">
          <AvatarFallback className="bg-muted-foreground text-secondary font-bold rounded-lg">U</AvatarFallback>
        </Avatar>
      )}
    </motion.div>
  );
}
