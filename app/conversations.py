"""Conversation memory: remembers each chat's messages so the model can see earlier turns.

Conversations are kept in the server's memory, so they are lost when the server restarts.
"""

import uuid

# Only the most recent messages are sent to the model: 20 messages = 10 back-and-forth turns.
# This keeps requests small and fast, and stays within the model's context limit.
MAX_MESSAGES = 20

# conversation ID -> list of messages, e.g. {"a1b2...": [{"role": "user", "content": "Hi"}, ...]}
_conversations: dict[str, list[dict]] = {}


def new_conversation_id() -> str:
    """Create a random, hard-to-guess ID for a new conversation."""
    return uuid.uuid4().hex


def get_history(conversation_id: str) -> list[dict]:
    """Return the conversation so far (an empty list if the ID is unknown)."""
    return _conversations.get(conversation_id, [])


def save_turn(conversation_id: str, user_message: dict, assistant_message: dict) -> None:
    """Add one question and its answer, keeping only the most recent messages."""
    history = get_history(conversation_id) + [user_message, assistant_message]
    _conversations[conversation_id] = history[-MAX_MESSAGES:]
