from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import Base


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id = Column(String(50), primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan", order_by="ChatMessage.created_at")
    user = relationship("User")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(String(50), ForeignKey("chat_conversations.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False) # 'user', 'model', 'system', 'tool'
    content = Column(Text, nullable=True)
    tool_calls = Column(JSON, nullable=True) # To store tool calls from the model
    tool_results = Column(JSON, nullable=True) # To store tool outputs
    created_at = Column(TIMESTAMP, server_default=func.now())

    conversation = relationship("ChatConversation", back_populates="messages")
