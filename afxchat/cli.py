#!/usr/bin/env python3
"""AfxChat - a small CLI client for an LM Studio server on the LAN."""

from __future__ import annotations

import configparser
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP_NAME = "AfxChat"
DEFAULT_HOST = "192.168.0.100"
DEFAULT_PORT = 1234
DEFAULT_TIMEOUT = 120
BOLD = "\033[1m"
RESET = "\033[0m"


def default_config_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "afxchat" / "config.ini"
    return Path.home() / ".config" / "afxchat" / "config.ini"


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model: str = ""
    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int = 40
    min_p: float = 0.05
    repeat_penalty: float = 1.05
    max_output_tokens: int = 1024
    context_length: int = 8192
    reasoning: str = "off"
    system_prompt: str = ""
    timeout: int = DEFAULT_TIMEOUT
    api_token: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_parser(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser["server"] = {
            "host": self.host,
            "port": str(self.port),
            "timeout": str(self.timeout),
            "api_token": self.api_token,
        }
        parser["chat"] = {
            "model": self.model,
            "temperature": str(self.temperature),
            "top_p": str(self.top_p),
            "top_k": str(self.top_k),
            "min_p": str(self.min_p),
            "repeat_penalty": str(self.repeat_penalty),
            "max_output_tokens": str(self.max_output_tokens),
            "context_length": str(self.context_length),
            "reasoning": self.reasoning,
            "system_prompt": self.system_prompt,
        }
        return parser


def load_config(path: Path) -> Config:
    cfg = Config()
    if not path.exists():
        save_config(path, cfg)
        return cfg

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")

    server = parser["server"] if parser.has_section("server") else {}
    chat = parser["chat"] if parser.has_section("chat") else {}

    def get(section: Any, key: str, default: str) -> str:
        return section.get(key, default)

    try:
        cfg.host = get(server, "host", cfg.host)
        cfg.port = int(get(server, "port", str(cfg.port)))
        cfg.timeout = int(get(server, "timeout", str(cfg.timeout)))
        cfg.api_token = get(server, "api_token", cfg.api_token).strip()

        cfg.model = get(chat, "model", cfg.model).strip()
        cfg.temperature = float(get(chat, "temperature", str(cfg.temperature)))
        cfg.top_p = float(get(chat, "top_p", str(cfg.top_p)))
        cfg.top_k = int(get(chat, "top_k", str(cfg.top_k)))
        cfg.min_p = float(get(chat, "min_p", str(cfg.min_p)))
        cfg.repeat_penalty = float(get(chat, "repeat_penalty", str(cfg.repeat_penalty)))
        cfg.max_output_tokens = int(
            get(chat, "max_output_tokens", str(cfg.max_output_tokens))
        )
        cfg.context_length = int(get(chat, "context_length", str(cfg.context_length)))
        cfg.reasoning = get(chat, "reasoning", cfg.reasoning).strip().lower()
        cfg.system_prompt = get(chat, "system_prompt", cfg.system_prompt)
    except ValueError as exc:
        raise ValueError(f"Invalid value in {path}: {exc}") from exc

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    if cfg.port < 1 or cfg.port > 65535:
        raise ValueError("port must be between 1 and 65535")
    if cfg.timeout < 1:
        raise ValueError("timeout must be greater than 0")
    if not 0 <= cfg.temperature <= 1:
        raise ValueError("temperature must be between 0 and 1 for LM Studio /api/v1/chat")
    if not 0 <= cfg.top_p <= 1:
        raise ValueError("top_p must be between 0 and 1")
    if cfg.top_k < 0:
        raise ValueError("top_k must be 0 or greater")
    if not 0 <= cfg.min_p <= 1:
        raise ValueError("min_p must be between 0 and 1")
    if cfg.repeat_penalty <= 0:
        raise ValueError("repeat_penalty must be greater than 0")
    if cfg.max_output_tokens < 1:
        raise ValueError("max_output_tokens must be greater than 0")
    if cfg.context_length < 1:
        raise ValueError("context_length must be greater than 0")
    if cfg.reasoning not in {"off", "on", "low", "medium", "high"}:
        raise ValueError("reasoning must be off, on, low, medium, or high")


def save_config(path: Path, cfg: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = cfg.to_parser()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        parser.write(fh)
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


class LMStudioError(RuntimeError):
    pass


class WaitingIndicator:
    """Show a small animated indicator while a blocking API request is running."""

    def __init__(self, message: str = "Waiting for LM Studio"):
        self.message = message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        frames = ("|", "/", "-", "\\")
        index = 0
        while not self._stop.is_set():
            print(f"\r{self.message} {frames[index]}", end="", flush=True)
            index = (index + 1) % len(frames)
            self._stop.wait(0.12)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        print("\r\033[2K", end="", flush=True)


class LMStudioClient:
    def __init__(self, config: Config):
        self.config = config

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.config.base_url + endpoint
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LMStudioError(f"HTTP {exc.code} from {endpoint}: {detail}") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise LMStudioError(
                f"Could not reach LM Studio at {url}. Check that LM Studio is listening on the LAN."
                f" Details: {reason}"
            ) from exc
        except TimeoutError as exc:
            raise LMStudioError(f"Request timed out after {self.config.timeout}s: {url}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LMStudioError(f"LM Studio returned invalid JSON from {endpoint}: {raw[:300]}") from exc

    def list_models(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v1/models")
        models = result.get("models")
        if not isinstance(models, list):
            raise LMStudioError("Unexpected /api/v1/models response: missing 'models' list")
        return [m for m in models if isinstance(m, dict)]

    def chat(self, user_input: str, previous_response_id: str | None) -> tuple[str, str | None]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": user_input,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "min_p": self.config.min_p,
            "repeat_penalty": self.config.repeat_penalty,
            "max_output_tokens": self.config.max_output_tokens,
            "context_length": self.config.context_length,
            "reasoning": self.config.reasoning,
            "store": True,
        }
        if self.config.system_prompt:
            payload["system_prompt"] = self.config.system_prompt
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id

        result = self._request("POST", "/api/v1/chat", payload)
        outputs = result.get("output", [])
        if not isinstance(outputs, list):
            raise LMStudioError("Unexpected /api/v1/chat response: missing 'output' list")

        messages = [
            item.get("content", "")
            for item in outputs
            if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), str)
        ]
        text = "\n".join(part for part in messages if part).strip()
        if not text:
            raise LMStudioError("LM Studio returned no assistant message in the response")

        response_id = result.get("response_id")
        if response_id is not None and not isinstance(response_id, str):
            response_id = None
        return text, response_id


def print_models(models: list[dict[str, Any]], current_model: str) -> None:
    llms = [m for m in models if m.get("type") == "llm"]
    if not llms:
        print("No LLM models were returned by LM Studio.")
        return

    print("Available LLM models:")
    for model in llms:
        key = str(model.get("key", "?"))
        display = str(model.get("display_name", key))
        params = str(model.get("params_string", ""))
        quant = model.get("quantization") or {}
        quant_name = quant.get("name") if isinstance(quant, dict) else None
        loaded = bool(model.get("loaded_instances"))
        marker = " *" if key == current_model else ""
        suffix_parts = [p for p in (params, quant_name, "loaded" if loaded else "") if p]
        suffix = f" [{', '.join(suffix_parts)}]" if suffix_parts else ""
        print(f"  {key}{marker} - {display}{suffix}")


def print_current_model(model: str) -> None:
    print(f"Current LLM model: {model}")


def choose_model(client: LMStudioClient, cfg: Config, config_path: Path, requested: str) -> None:
    models = client.list_models()
    llms = [m for m in models if m.get("type") == "llm"]
    by_key = {str(m.get("key")): m for m in llms}

    if requested not in by_key:
        print(f"Model not found: {requested}", file=sys.stderr)
        print_models(models, cfg.model)
        return

    cfg.model = requested
    save_config(config_path, cfg)
    print(f"Model set to: {cfg.model}")


def print_help() -> None:
    print(
        "Commands:\n"
        "  /model                  Print the model currently used\n"
        "  /model list             List available LLM models\n"
        "  /model set <model-name> Select and save the model\n"
        "  /help                   Show this help\n"
        "  /quit                   Exit AfxChat\n"
        "\n"
        "Anything else is sent to the configured model."
    )


def main() -> int:
    config_path = default_config_path()
    try:
        cfg = load_config(config_path)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    client = LMStudioClient(cfg)
    previous_response_id: str | None = None

    print(f"{APP_NAME} - LM Studio LAN chat client")
    print(f"Server: {cfg.base_url}")
    print(f"Config: {config_path}")
    print(f"Model:  {cfg.model or '(not selected)'}")
    print("Type /help for commands.")

    while True:
        try:
            user_input = input(f"\n{BOLD}You>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if not user_input:
            continue

        if user_input == "/quit":
            print("Goodbye.")
            return 0

        if user_input == "/help":
            print_help()
            continue

        if user_input == "/model":
            try:
                print_current_model(cfg.model)
            except LMStudioError as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        if user_input == "/model list":
            try:
                print_models(client.list_models(), cfg.model)
            except LMStudioError as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        if user_input.startswith("/model set "):
            requested = user_input[len("/model set "):].strip()
            if not requested:
                print("Usage: /model set <model-name>")
                continue
            try:
                choose_model(client, cfg, config_path, requested)
                previous_response_id = None
            except LMStudioError as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        if user_input == "/model":
            print("Usage: /model list | /model set <model-name>")
            continue

        if user_input.startswith("/"):
            print(f"Unknown command: {user_input}. Type /help for commands.")
            continue

        if not cfg.model:
            print("No model selected. Run /model list, then /model set <model-name>.")
            continue

        indicator = WaitingIndicator()
        try:
            indicator.start()
            answer, previous_response_id = client.chat(user_input + cfg.system_prompt, previous_response_id)
        except LMStudioError as exc:
            print(f"\r\033[2KError: {exc}", file=sys.stderr)
        finally:
            indicator.stop()
        if 'answer' in locals():
            print(f"{BOLD}AfxChat>{RESET} {answer}")
            del answer


if __name__ == "__main__":
    raise SystemExit(main())
