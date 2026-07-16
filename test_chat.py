"""
test_chat.py
=============
Simple terminal REPL for manually testing conversation_manager.py before
wiring up the FastAPI layer (Module H). Simulates one continuous session.

Run: python test_chat.py

Type 'quit' to exit, 'reset' to start a fresh session without restarting
the script.

Try walking through:
  - A general question ("what services do you offer")
  - A full booking flow ("I'd like to book a call" -> answer each prompt)
  - Changing your mind mid-confirmation ("no wait, change the time")
  - Abandoning a booking ("actually never mind")
  - A cancellation flow (need a real booking ID or email from a prior booking)
"""

from conversation_manager import handle_message, reset_session, get_greeting

SESSION_ID = "cli-test-session"


def main():
    print("Chatbot test REPL. Type 'quit' to exit, 'reset' to restart the session.\n")

    greeting = get_greeting(SESSION_ID)
    if greeting:
        print(f"Bot: {greeting}\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if user_input.lower() == "quit":
            break
        if user_input.lower() == "reset":
            reset_session(SESSION_ID)
            print("(session reset)\n")
            greeting = get_greeting(SESSION_ID)
            if greeting:
                print(f"Bot: {greeting}\n")
            continue
        if not user_input:
            continue

        result = handle_message(SESSION_ID, user_input)
        print(f"Bot [{result['state']}]: {result['reply']}\n")


if __name__ == "__main__":
    main()