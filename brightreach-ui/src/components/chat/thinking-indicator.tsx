"use client";

import { motion } from "framer-motion";

export function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-2 py-6">
      <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center shrink-0 mt-1 shadow-sm">
        <span className="text-white font-bold text-sm">B</span>
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1.5 px-3 py-2 bg-muted/50 rounded-2xl rounded-tl-sm w-fit">
          <span className="text-xs text-muted-foreground font-medium mr-1">BrightReach is thinking</span>
          <motion.div
            className="w-1.5 h-1.5 rounded-full bg-indigo-500"
            animate={{ y: [0, -3, 0] }}
            transition={{ duration: 0.6, repeat: Infinity, delay: 0 }}
          />
          <motion.div
            className="w-1.5 h-1.5 rounded-full bg-indigo-500"
            animate={{ y: [0, -3, 0] }}
            transition={{ duration: 0.6, repeat: Infinity, delay: 0.2 }}
          />
          <motion.div
            className="w-1.5 h-1.5 rounded-full bg-indigo-500"
            animate={{ y: [0, -3, 0] }}
            transition={{ duration: 0.6, repeat: Infinity, delay: 0.4 }}
          />
        </div>
      </div>
    </div>
  );
}
