export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000/api";

export async function getNewSession(): Promise<{ session_id: string }> {
  const res = await fetch(`${API_BASE_URL}/new-session`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error("Failed to create session");
  return res.json();
}

export async function getGreeting(sessionId: string): Promise<{ greeting: string | null }> {
  const res = await fetch(`${API_BASE_URL}/greeting?session_id=${sessionId}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error("Failed to fetch greeting");
  return res.json();
}

export async function sendChatMessage(sessionId: string, message: string): Promise<{ reply: string; state: string }> {
  const res = await fetch(`${API_BASE_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok) throw new Error("Failed to send message");
  return res.json();
}
