import asyncio
import json
import logging
import os
import shutil
from contextvars import ContextVar
from pathlib import Path

import requests

from econagents.ws.client import WebSocketClient

HOSTNAME = "188.166.34.67"
LOG_PATH = Path(__file__).parent / "logs"

ctx_agent_id = ContextVar("agent_id", default="N/A")


class ContextInjectingFilter(logging.Filter):
    def filter(self, record):
        record.agent_id = ctx_agent_id.get()
        return True


class RecoveryCodeError(Exception):
    """Exception raised when failing to get recovery code."""

    pass


def get_recovery_code(hostname, game_id):
    url = f"http://{hostname}/api/v1/games/get-recovery"
    params = {"game_id": game_id}
    response = requests.get(url, params=params, timeout=30)

    if response.status_code == 200:
        data = response.json()
        return data["data"]["recovery"]

    raise RecoveryCodeError(f"Failed to get recovery code: {response.text}")


def get_agent_logger(agent_id, game_id):
    agent_log_file = LOG_PATH / "agents" / f"agent_{agent_id}.log"
    game_log_file = LOG_PATH / "game" / f"game_{game_id}.log"

    agent_logger = logging.getLogger(f"agent_{agent_id}")
    agent_logger.setLevel(logging.DEBUG)

    if agent_log_file.exists():
        agent_log_file.unlink()

    Path(agent_log_file).touch()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [AGENT %(agent_id)s] %(message)s")

    file_handler = logging.FileHandler(agent_log_file)
    file_handler.setFormatter(formatter)
    game_file_handler = logging.FileHandler(game_log_file)
    game_file_handler.setFormatter(formatter)

    context_filter = ContextInjectingFilter()
    file_handler.addFilter(context_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    agent_logger.addHandler(file_handler)
    agent_logger.addHandler(console_handler)
    agent_logger.addHandler(game_file_handler)

    return agent_logger


def get_game_logger(game_id):
    game_log_file = LOG_PATH / "game" / f"game_{game_id}.log"

    if game_log_file.exists():
        game_log_file.unlink()

    Path(game_log_file).touch()

    game_logger = logging.getLogger(f"game_{game_id}")
    game_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(game_log_file)
    file_handler.setFormatter(formatter)

    context_filter = ContextInjectingFilter()
    file_handler.addFilter(context_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    game_logger.addHandler(file_handler)
    game_logger.addHandler(console_handler)

    return game_logger


async def start_simulation(ws_url, game_id, recovery, agent_id):
    agent_logger = get_agent_logger(agent_id, game_id)
    ctx_agent_id.set(agent_id)

    try:
        agent_logger.info(f"Connecting to WebSocket URL: {ws_url}")
        client = WebSocketClient(url=ws_url, game_id=game_id, recovery=recovery, logger=agent_logger)
        await client.start()

    except Exception:
        agent_logger.exception(f"Error in simulation for Agent {agent_id}")
        raise


def validate_game_params(game_id, agents, game_type, use_config):
    if not game_id:
        raise ValueError("Missing required parameter: 'game_id'")
    if not agents:
        raise ValueError("Missing required parameter: 'agents'")
    if not game_type:
        raise ValueError("Missing required parameter: 'game_type'")


def save_game_config(game_config):
    with Path(LOG_PATH / "game" / f"game_{game_config['game_id']}.json").open("w") as f:
        json.dump(game_config, f, indent=4)


def load_game_config(game_id):
    with Path(LOG_PATH / "game" / f"game_{game_id}.json").open("r") as f:
        return json.load(f)


def get_game_config(game_id, agents, game_type, use_config="auto"):
    if use_config == "auto":
        if Path(LOG_PATH / "game" / f"game_{game_id}.json").exists():
            game_config = load_game_config(game_id)
        else:
            game_config = {"game_id": game_id, "agents": agents, "game_type": game_type, "recovery_codes": []}
            game_config["recovery_codes"] = [get_recovery_code(HOSTNAME, game_id) for _ in range(agents)]
            save_game_config(game_config)
    elif use_config:
        if Path(LOG_PATH / "game" / f"game_{game_id}.json").exists():
            game_config = load_game_config(game_id)
        else:
            raise ValueError("Game config provided but no config file exists")
    else:
        game_config = {"game_id": game_id, "agents": agents, "game_type": game_type, "recovery_codes": []}
        game_config["recovery_codes"] = [get_recovery_code(HOSTNAME, game_id) for _ in range(agents)]
        save_game_config(game_config)

    return game_config


async def spawn_agents(game_id, agents, game_type, use_config="auto"):
    game_logger = get_game_logger(game_id)
    game_logger.info(f"Spawning {agents} agents for game {game_id}")

    try:
        validate_game_params(game_id, agents, game_type, use_config)

        game_config = get_game_config(game_id, agents, game_type, use_config)
        game_logger.info(f"Game config: {game_config}")

        ws_url = f"ws://{HOSTNAME}:3088/wss"
        tasks = []
        game_logger.info("Starting simulations")

        for idx, recovery_code in enumerate(game_config["recovery_codes"]):
            tasks.append(start_simulation(ws_url, game_id, recovery_code, idx + 1))
        await asyncio.gather(*tasks)

    except Exception:
        game_logger.exception("Error in game setup")
        raise

    finally:
        game_logger.info("Game finished")


if __name__ == "__main__":
    Path(LOG_PATH).mkdir(parents=True, exist_ok=True)
    Path(LOG_PATH / "agents").mkdir(parents=True, exist_ok=True)
    Path(LOG_PATH / "game").mkdir(parents=True, exist_ok=True)

    asyncio.run(spawn_agents(game_id=163, agents=6, game_type="harberger"))
