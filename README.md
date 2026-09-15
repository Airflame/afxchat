# AfxChat

A small Python CLI for chatting with an LM Studio server running on another computer in your LAN.

Default server target: `http://192.168.0.100:1234`

## Requirements

- Linux Mint / Linux with Python 3.10+
- LM Studio running its server on host computer
- The LM Studio server must accept LAN connections

AfxChat uses only the Python standard library at runtime. No `requests` or other third-party package is required.

## Install on client computer

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Then run:

```bash
afxchat
```

For a one-off run without installing the package:

```bash
python3 -m afxchat.cli
```

## Commands

```text
/model list
/model set <model-name>
/help
/quit
```

The exact model key is the value printed by `/model list`.

## Configuration

The config is stored at:

```text
~/.config/afxchat/config.ini
```

or under `$XDG_CONFIG_HOME/afxchat/config.ini` when `XDG_CONFIG_HOME` is set.

A sample is included as `config.example.ini`.

The selected model is saved automatically when `/model set ...` is used. Generation parameters such as temperature, top-p, context length, and output-token limit are also read from this file.

## Conversation state

AfxChat uses LM Studio's stateful `/api/v1/chat` API. After a successful response, the returned `response_id` is sent with the next message as `previous_response_id`, so the conversation continues without resending the full transcript.

Changing models resets the current conversation chain.

## LAN setup

On host computer, make sure LM Studio's server is enabled and configured to listen on the network rather than only on `localhost`. If authentication is enabled, put the API token in `[server] api_token`.

You may also need to allow TCP port `1234` through the firewall on computer #1.

## Security note

The config file is created with permissions `0600` where the operating system permits it because it can contain an API token.
