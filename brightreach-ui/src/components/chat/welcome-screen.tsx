"use client";

import React from "react";
import { motion } from "framer-motion";
import { Sparkles, Calendar, BookOpen, DollarSign, TrendingUp, Search } from "lucide-react";
import { Card } from "@/components/ui/card";

const SUGGESTIONS = [
  { icon: Search, title: "Explain SEO Packages", desc: "Learn how we can boost your rankings." },
  { icon: Calendar, title: "Book Discovery Call", desc: "Schedule a free consultation with our experts." },
  { icon: BookOpen, title: "What services do you offer?", desc: "Overview of our digital marketing services." },
  { icon: DollarSign, title: "Pricing Information", desc: "View our transparent pricing tiers." },
  { icon: TrendingUp, title: "Generate Marketing Strategy", desc: "Get a custom AI-generated plan." },
  { icon: Sparkles, title: "How does PPC work?", desc: "Understand pay-per-click advertising." },
];

export function WelcomeScreen({ onSelect }: { onSelect: (text: string) => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full w-full max-w-3xl mx-auto px-4 pt-10 pb-20">
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="text-center mb-10"
      >
        <div className="w-16 h-16 mx-auto bg-gradient-to-br from-indigo-500 to-purple-600 rounded-2xl flex items-center justify-center shadow-lg shadow-indigo-500/20 mb-6">
          <Sparkles className="w-8 h-8 text-white" />
        </div>
        <h1 className="text-3xl font-semibold tracking-tight mb-2">How can BrightReach help today?</h1>
        <p className="text-muted-foreground">Your AI-powered digital marketing assistant</p>
      </motion.div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 w-full">
        {SUGGESTIONS.map((s, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: i * 0.1 }}
          >
            <Card
              className="p-4 cursor-pointer hover:bg-muted/50 transition-all hover:shadow-md hover:border-indigo-200 dark:hover:border-indigo-800 flex items-start gap-3 group"
              onClick={() => onSelect(s.title)}
            >
              <div className="p-2 bg-indigo-50 dark:bg-indigo-950/50 text-indigo-600 dark:text-indigo-400 rounded-lg group-hover:scale-110 transition-transform">
                <s.icon className="w-5 h-5" />
              </div>
              <div className="flex flex-col">
                <span className="font-medium text-sm text-foreground">{s.title}</span>
                <span className="text-xs text-muted-foreground">{s.desc}</span>
              </div>
            </Card>
          </motion.div>
        ))}
      </div>
    </div>
  );
}
