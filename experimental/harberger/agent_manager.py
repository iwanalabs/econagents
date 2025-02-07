import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Optional

from econagents.core.websocket import WebSocketClient
from econagents.llm.openai import ChatOpenAI
from experimental.harberger.agents import Agent, Developer, Owner, Speculator
from experimental.harberger.models import Message


class AgentManager(WebSocketClient):
    name: Optional[str] = None
    role: Optional[str] = None

    def __init__(self, url: str, login_payload: dict[str, Any], game_id: int, logger: logging.Logger):
        self.game_id = game_id
        self.login_payload = login_payload
        self.logger = logger
        self._agent: Optional[Agent] = None
        self.llm = ChatOpenAI()
        super().__init__(url, login_payload, game_id, logger)

    @property
    def agent(self) -> Optional[Agent]:
        return self._agent  # Return the cached agent if already initialized

    def _initialize_agent(self) -> Agent:
        """
        Create and cache the agent instance based on the assigned role.
        """
        if self._agent is None:
            if self.role == 1:
                self._agent = Speculator(llm=self.llm, game_id=self.game_id, logger=self.logger)
            elif self.role == 2:
                self._agent = Developer(llm=self.llm, game_id=self.game_id, logger=self.logger)
            elif self.role == 3:
                self._agent = Owner(llm=self.llm, game_id=self.game_id, logger=self.logger)
            else:
                self.logger.error("Invalid role assigned; cannot initialize agent.")
                raise ValueError("Invalid role for agent initialization.")
        return self._agent

    def _extract_message_data(self, message: str) -> Optional[Message]:
        try:
            msg = json.loads(message)
            msg_type = msg.get("type", "")
            event_type = msg.get("eventType", "")
            data = msg.get("data", {})
        except json.JSONDecodeError:
            self.logger.error("Invalid JSON received.")
            return None
        return Message(msg_type=msg_type, event_type=event_type, data=data)

    async def on_message(self, message):
        self.logger.debug(f"← Received: {message}")

        msg = self._extract_message_data(message)

        if not msg:
            return

        if msg.msg_type == "event" and self.agent:
            self.agent.update_state(msg)

        if msg.msg_type == "event" and msg.event_type == "assign-name":
            self.name = msg.data.get("name")
            self.logger.info(f"Name assigned: {self.name}")
            await self._send_player_ready()

        elif msg.msg_type == "event" and msg.event_type == "assign-role":
            self.role = msg.data.get("role", None)
            self.logger.info(f"Role assigned: {self.role}")
            self._initialize_agent()
            self.agent.update_state(msg)

        elif msg.msg_type == "event" and msg.event_type == "players-known":
            self.known_players = msg.data.get("players", [])
            self.logger.info(f"Known players: {self.known_players}")

        elif msg.msg_type == "event" and msg.event_type == "phase-transition":
            self.current_phase = msg.data.get("phase")
            round_number = msg.data.get("round", 1)
            self.logger.info(f"Transitioning to phase {self.current_phase}, round {round_number}")
            await self._handle_phase(self.current_phase)

    async def _handle_phase(self, phase):
        """
        Execute a simple auto-action per phase
        0 - Name assignment: All players must send "player-is-ready".
        2 - Declaration: Owners (role=3) or Developers (role=2) declare random property values.
        3 - Speculation: Speculators (role=1) might snipe random owners.
        6 - Market: (Optional) we let players place a random "ask" order.
        7 - Declaration again.
        8 - Speculation again.
        Phases 1, 4, 5, 9 do nothing in this minimal example.
        """
        # Phase 2 => if role=Owner(3) or Developer(2), declare random values
        if phase == 2 and self.role in [3, 2]:
            result = await self.agent.handle_phase(phase)
            await self.send_message(json.dumps(result))
            self.logger.info(f"Declared values: {result['declaration']}")

        # Phase 3 => if role=Speculator(1), try a random speculation
        elif phase == 3 and self.role == 1:
            result = await self.agent.handle_phase(phase)
            await self.send_message(json.dumps(result))
            self.logger.info(f"Speculating against targets: {result}")

        # Phase 6 => Market: if role=Speculator(1), place a random ask order
        elif phase == 6 and self.role == 1:
            if self.role == 1:
                post_msg = {
                    "gameId": self.game_id,
                    "type": "post-order",
                    "order": {
                        "price": 100,
                        "quantity": 4,
                        "condition": self.agent.state.winning_condition,
                        "type": "ask",
                        "now": False,
                    },
                }
            elif self.role == 2:
                post_msg = {
                    "gameId": self.game_id,
                    "type": "post-order",
                    "order": {
                        "price": 100,
                        "quantity": 2,
                        "condition": self.agent.state.winning_condition,
                        "type": "bid",
                        "now": False,
                    },
                }
            else:
                await asyncio.sleep(1)
                post_msg = {
                    "gameId": self.game_id,
                    "type": "post-order",
                    "order": {
                        "price": 100,
                        "quantity": 1,
                        "condition": self.agent.state.winning_condition,
                        "type": "bid",
                        "now": True,
                    },
                }
            await self.send_message(json.dumps(post_msg))
            self.logger.info(f"Posted market order: {post_msg['order']}")

        # Phase 7 => declare again if role=Owner(3) or Developer(2)
        elif phase == 7 and self.role in [2, 3]:
            result = await self.agent.handle_phase(phase)
            await self.send_message(json.dumps(result))
            self.logger.info(f"Declared (second time): {result['declaration']}")

        # Phase 8 => speculation again if role=Speculator(1)
        elif phase == 8 and self.role == 1:
            potential_targets = [p["number"] for p in self.known_players if p["role"] in [2, 3]]
            chosen = []
            for t in potential_targets:
                if random.random() < 0.5:
                    chosen.append(t)
            snipe_msg = {
                "gameId": self.game_id,
                "type": "done-speculating",
                "snipe": [
                    chosen,  # condition 0
                    [],
                ],
            }
            await self.send_message(json.dumps(snipe_msg))
            self.logger.info(f"Final speculation against: {chosen}")

        # Phases 1,4,5,9 => do nothing in this demo

    async def _send_player_ready(self):
        """
        Send the 'player-is-ready' message so that the game can advance from the introduction.
        """
        ready_msg = {"gameId": self.game_id, "type": "player-is-ready"}
        await self.send_message(json.dumps(ready_msg))
        self.logger.info("Sent player-is-ready message.")
