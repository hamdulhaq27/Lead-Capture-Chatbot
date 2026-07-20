"use client";

import React from "react";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function MarketingCard({
  title,
  description,
  icon: Icon,
}: {
  title: string;
  description: string;
  icon: React.ElementType;
}) {
  return (
    <Card className="p-5 flex flex-col gap-3 group hover:border-indigo-200 dark:hover:border-indigo-800 transition-colors shadow-sm">
      <div className="w-12 h-12 rounded-xl bg-indigo-50 dark:bg-indigo-950/50 flex items-center justify-center text-indigo-600 dark:text-indigo-400 group-hover:scale-110 transition-transform">
        <Icon className="w-6 h-6" />
      </div>
      <div>
        <h4 className="font-semibold text-lg">{title}</h4>
        <p className="text-sm text-muted-foreground mt-1 leading-relaxed">{description}</p>
      </div>
      <Button variant="ghost" className="w-fit p-0 h-auto mt-2 text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 hover:bg-transparent group/btn">
        Learn More <ArrowRight className="w-4 h-4 ml-1 group-hover/btn:translate-x-1 transition-transform" />
      </Button>
    </Card>
  );
}

export function PricingCard({
  tier,
  price,
  description,
  features,
  highlight = false,
}: {
  tier: string;
  price: string;
  description: string;
  features: string[];
  highlight?: boolean;
}) {
  return (
    <Card className={`p-6 flex flex-col h-full relative overflow-hidden ${highlight ? "border-indigo-500 shadow-md" : "shadow-sm"}`}>
      {highlight && (
        <div className="absolute top-0 right-0 bg-indigo-500 text-white text-[10px] font-bold px-3 py-1 rounded-bl-lg uppercase tracking-wider">
          Most Popular
        </div>
      )}
      <div className="mb-4">
        <h4 className="font-semibold text-xl text-foreground">{tier}</h4>
        <p className="text-sm text-muted-foreground mt-1 h-10">{description}</p>
      </div>
      <div className="mb-6 flex items-baseline gap-1">
        <span className="text-3xl font-bold">{price}</span>
        {price !== "Custom" && <span className="text-muted-foreground text-sm">/mo</span>}
      </div>
      <div className="flex flex-col gap-3 flex-1 mb-6">
        {features.map((f, i) => (
          <div key={i} className="flex items-start gap-2 text-sm text-muted-foreground">
            <CheckCircle2 className="w-4 h-4 text-indigo-500 shrink-0 mt-0.5" />
            <span>{f}</span>
          </div>
        ))}
      </div>
      <Button className={`w-full ${highlight ? "bg-indigo-600 hover:bg-indigo-700" : ""}`} variant={highlight ? "default" : "outline"}>
        Book Consultation
      </Button>
    </Card>
  );
}
