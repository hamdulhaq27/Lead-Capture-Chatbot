"use client";

import React from "react";
import { Calendar, Clock, User, CheckCircle2, XCircle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function BookingSuccessCard({
  bookingId,
  customerName,
  date,
  time,
}: {
  bookingId: string;
  customerName: string;
  date: string;
  time: string;
}) {
  return (
    <Card className="p-5 max-w-sm border-green-200 dark:border-green-900 bg-green-50/50 dark:bg-green-950/20 shadow-sm relative overflow-hidden">
      <div className="absolute top-0 right-0 w-24 h-24 bg-green-500/10 rounded-bl-full -z-10" />
      <div className="flex items-start gap-4">
        <div className="w-10 h-10 rounded-full bg-green-100 dark:bg-green-900 flex items-center justify-center shrink-0">
          <CheckCircle2 className="w-6 h-6 text-green-600 dark:text-green-400" />
        </div>
        <div className="flex flex-col gap-1 w-full">
          <h3 className="font-semibold text-green-900 dark:text-green-100 text-lg">Booking Confirmed</h3>
          <p className="text-sm text-green-700/80 dark:text-green-300/80 mb-3">ID: #{bookingId}</p>
          
          <div className="flex flex-col gap-2 text-sm bg-white dark:bg-background p-3 rounded-lg border border-green-100 dark:border-green-900 shadow-sm">
            <div className="flex items-center gap-2 text-muted-foreground">
              <User className="w-4 h-4" />
              <span className="text-foreground font-medium">{customerName}</span>
            </div>
            <div className="flex items-center gap-2 text-muted-foreground">
              <Calendar className="w-4 h-4" />
              <span className="text-foreground">{date}</span>
            </div>
            <div className="flex items-center gap-2 text-muted-foreground">
              <Clock className="w-4 h-4" />
              <span className="text-foreground">{time}</span>
            </div>
          </div>

          <div className="flex gap-2 mt-4">
            <Button variant="outline" size="sm" className="w-full h-8 text-xs bg-white dark:bg-background">
              Reschedule
            </Button>
            <Button variant="outline" size="sm" className="w-full h-8 text-xs text-red-600 hover:text-red-700 hover:bg-red-50 dark:bg-background">
              Cancel
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}

export function CancellationCard({ reason }: { reason: string }) {
  return (
    <Card className="p-4 max-w-sm border-red-200 dark:border-red-900 bg-red-50/50 dark:bg-red-950/20 flex gap-3">
      <div className="mt-0.5">
        <XCircle className="w-5 h-5 text-red-500" />
      </div>
      <div className="flex flex-col">
        <h4 className="font-medium text-red-900 dark:text-red-100">Booking Cancelled</h4>
        <p className="text-sm text-red-700/80 dark:text-red-300/80 mt-1">{reason}</p>
        <Button variant="outline" size="sm" className="mt-3 w-fit h-8 text-xs bg-white dark:bg-background">
          Book Another Meeting
        </Button>
      </div>
    </Card>
  );
}
