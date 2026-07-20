"use client";

import React, { useState, useRef, useEffect } from "react";
import { ChatHeader } from "./chat-header";
import { ChatInput } from "./chat-input";
import { MessageBubble } from "./message-bubble";
import { WelcomeScreen } from "./welcome-screen";
import { ThinkingIndicator } from "./thinking-indicator";
import { ChatMessage } from "@/types";
import { getNewSession, getGreeting, sendChatMessage } from "@/services/api";
import { MarketingCard, PricingCard } from "@/components/ui/marketing-card";
import { BookingSuccessCard, CancellationCard } from "@/components/booking/booking-card";
import { BarChart, Search, Megaphone } from "lucide-react";

export function ChatArea() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isThinking, setIsThinking] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Initialize session and get greeting
  useEffect(() => {
    async function initSession() {
      try {
        const { session_id } = await getNewSession();
        setSessionId(session_id);
        const { greeting } = await getGreeting(session_id);
        if (greeting) {
          setMessages([
            {
              id: Date.now().toString(),
              role: "assistant",
              content: greeting,
              timestamp: new Date(),
              isStreaming: true,
            },
          ]);
        }
      } catch (error) {
        console.error("Failed to initialize chat session", error);
      }
    }
    initSession();
  }, []);

  const handleSend = async (text: string) => {
    if (!sessionId) return;

    const newUserMsg: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    
    setMessages((prev) => [...prev, newUserMsg]);
    setIsThinking(true);

    try {
      const response = await sendChatMessage(sessionId, text);
      const newAssistantMsg: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: "assistant",
        content: response.reply,
        timestamp: new Date(),
        isStreaming: true,
      };
      setMessages((prev) => [...prev, newAssistantMsg]);
    } catch (error) {
      console.error("Failed to send message", error);
      setMessages((prev) => [
        ...prev,
        {
          id: (Date.now() + 1).toString(),
          role: "assistant",
          content: "Sorry, I encountered an error communicating with the server. Please try again.",
          timestamp: new Date(),
        }
      ]);
    } finally {
      setIsThinking(false);
    }
  };

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isThinking]);

  // Helper to detect if a message should trigger custom cards
  const renderSpecialCards = (content: string) => {
    const lower = content.toLowerCase();
    
    if (lower.includes("pricing") && (lower.includes("tier") || lower.includes("package") || lower.includes("cost"))) {
      return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 ml-12 my-4">
          <PricingCard tier="Starter" price="$499" description="Perfect for small local businesses." features={["Basic SEO", "Google My Business", "Monthly Report"]} />
          <PricingCard tier="Growth" price="$999" description="For scaling businesses." features={["Advanced SEO", "Content Marketing", "PPC Management", "Bi-weekly sync"]} highlight />
          <PricingCard tier="Enterprise" price="Custom" description="Tailored solutions for large brands." features={["Dedicated Account Manager", "Full-funnel Strategy", "Custom Integrations"]} />
        </div>
      );
    }
    if ((lower.includes("service") || lower.includes("offer")) && lower.includes("marketing")) {
      return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 ml-12 my-4">
          <MarketingCard title="SEO Optimization" description="Rank higher on Google and drive organic traffic." icon={Search} />
          <MarketingCard title="PPC Advertising" description="High-converting ad campaigns on Google and Facebook." icon={Megaphone} />
          <MarketingCard title="Data Analytics" description="Deep insights into your marketing performance." icon={BarChart} />
        </div>
      );
    }
    if (lower.includes("booked") && lower.includes("confirmed")) {
      return (
        <div className="ml-12 my-4">
          <BookingSuccessCard bookingId="BR-8492" customerName="Alex Johnson" date="Oct 24, 2026" time="2:00 PM EST" />
        </div>
      );
    }
    if (lower.includes("cancel") && lower.includes("booking")) {
      return (
        <div className="ml-12 my-4">
          <CancellationCard reason="User requested cancellation." />
        </div>
      );
    }
    return null;
  };

  return (
    <div className="flex flex-col flex-1 h-screen bg-background relative overflow-hidden">
      <ChatHeader />
      
      <div className="flex-1 overflow-y-auto custom-scrollbar">
        {messages.length === 0 ? (
          <WelcomeScreen onSelect={handleSend} />
        ) : (
          <div className="max-w-3xl mx-auto px-4 py-6 flex flex-col min-h-full justify-end">
            {messages.map((msg) => (
              <div key={msg.id}>
                <MessageBubble message={msg} />
                {msg.role === "assistant" && renderSpecialCards(msg.content)}
              </div>
            ))}
            {isThinking && <ThinkingIndicator />}
            <div ref={scrollRef} className="h-4 shrink-0" />
          </div>
        )}
      </div>

      <ChatInput onSend={handleSend} disabled={isThinking || !sessionId} />
    </div>
  );
}
