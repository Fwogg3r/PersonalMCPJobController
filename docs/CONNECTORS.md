# Writing connectors

A connector is one class. pmjc finds connectors in four places:
1. built-in modules in `src/pmjc/connectors/`;
2. Python files in `<PMJC_HOME>/plugins/`;
3. YAML files in `<PMJC_HOME>/connectors.d/`;
4. installed packages exposing the `pmjc.connectors` entry point, if listed in `enabled_plugins`.

Run `pmjc --help` to see where `PMJC_HOME` is (`pmjc setup` prints it).

## Python

```python
from pmjc.connector import Connector, Event, Field, event_source, tool


class Todoist(Connector):
    id = "todoist"                         # also the prefix for tool names and credential keys
    name = "Todoist"
    description = "Your tasks."            # shown in the dashboard and to agents
    docs_url = "https://todoist.com/app/settings/integrations/developer"
    credentials = [Field("token", "API token")]   # secret=True by default; secret=False for plain settings

    async def _api(self, method, path, **kw):
        r = await self.http.request(method, "https://api.todoist.com/rest/v2" + path,
                                    headers={"Authorization": f"Bearer {self.secret('token')}"}, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    @tool(scope="read", untrusted_output=True)       # tasks may contain text from shared projects
    async def todoist_tasks(self, filter: str = "today") -> list[dict]:
        """Open tasks matching a Todoist filter."""   # the docstring is the tool description
        return [{"id": t["id"], "content": t["content"], "due": (t.get("due") or {}).get("date")}
                for t in await self._api("GET", "/tasks", params={"filter": filter})]

    @tool(scope="act")
    async def todoist_add(self, content: str, due: str | None = None) -> dict:
        """Add a task."""
        return await self._api("POST", "/tasks", json={"content": content, "due_string": due})
```

Rules:
- **Tool names** are `<connector id>_<verb>`, using letters, digits and underscores, at most 64 characters.
- **Parameters** need type hints. pmjc builds the JSON schema from them. Parameters starting with `_` are hidden. A parameter named `_caller` receives the calling `Caller`.
- **`scope`:**
  - `read`: no side effects.
  - `act`: changes something; needs approval. This is the default if you leave it out.
  - `jobs`: pmjc job management.
- **`untrusted_output=True`** when the result contains text other people wrote (messages, email, web pages). It taints the run.
- **`egress=True`** when model-chosen arguments leave the machine (search queries, URLs). Tainted runs can't call it.
- **Secrets** come from `self.secret(name)`, plain settings from `self.option(name, default)`, and HTTP from `self.http` (a shared `httpx.AsyncClient`). Never log or return secrets.

Optional extras:
- `@event_source(interval=60)`: a method that returns or yields `Event(...)` objects. pmjc dedupes them by `key` and triggers matching jobs. Use `self.state_get` and `self.state_set` for cursors.
- `async def notify(self, title, message, actions=None)`: makes the connector an output channel. `actions` are approve/deny links; prefix them with `config.load().public_url`.
- `async def health(self) -> Health`: shown in the dashboard and in `pmjc connectors`.
- OAuth sign-in: inherit `pmjc.oauth2.OAuth2Mixin`, set `AUTH_URL`, `TOKEN_URL` and `SCOPES`, and add `client_id` and `client_secret` fields. Call `await self.access_token()`. See `connectors/google.py`.
- `platforms = ["win32"]` limits a connector to some operating systems.

## YAML (plain HTTP APIs)

```yaml
id: jokes
name: JokeAPI
description: Random jokes.
base_url: https://v2.jokeapi.dev
auth: {type: none}            # none | bearer | header (+ header: X-Api-Key) | query (+ param: key) | basic
credentials: []               # e.g. [{name: api_key, label: API key}]
tools:
  - name: jokes_random
    description: A random joke.
    method: GET
    path: /joke/{category}
    scope: read
    untrusted_output: true
    params:
      category: {type: string, in: path, default: Programming}
      safe-mode: {type: string, in: query, default: ""}     # becomes safe_mode for agents
```

Defaults fail closed: `scope: act`, `untrusted_output: true`, and `egress: true` when the tool has parameters. Credentials are only ever sent to `base_url`; path parameters are URL-encoded, so they can't change the host.

## Command-line tools (YAML, `kind: command`)

Wrap tools you already use, such as scripts, CLIs or PowerShell, without writing Python. See [examples/connectors.d/command-tools.yaml](../examples/connectors.d/command-tools.yaml).
- Each tool is an `argv` list with `{param}` placeholders. It runs **without a shell**, so a value can't inject another command.
- Constrain every parameter with `pattern` (full-match regex) or `choices`. Values starting with `-` are refused unless the parameter says `allow_dash: true`.
- An optional placeholder that stands alone as an argument (`"{query}"`) is dropped when it isn't given.
- `ANTHROPIC_API_KEY` is removed from the environment the commands see. The output is capped.
- Defaults fail closed: `scope: act` and `untrusted_output: true`.

## Built-in local connectors
- `files`: read-only access to the folders in option `roots`. Secret-looking names are never listed or read (`*token*`, `*secret*`, `*.key`, `.env`, `id_*`, `*.db`, ...), and option `deny` adds more.
- `pc`: CPU, memory, disks, GPU and processes of the PC running pmjc.
- `ssh`: any machine you can reach with OpenSSH and a key, e.g. `pmjc ssh add nas --host 192.0.2.10 --user admin --key ~/.ssh/id_ed25519`. Optional `--port`, `--jump bastion.example.com` and `--proxy-command "cloudflared access ssh --hostname %h"`. `--alias` reuses a `~/.ssh/config` Host. Without configured machines, all `~/.ssh/config` aliases are available. Login is non-interactive (keys or agent, never passwords). `ssh_check` is read-only; `ssh_run` needs approval and returns within about `commands.wait_seconds` (default 8): a longer command returns `status: running` with a `run_id`, and `command_status(run_id)` gives the rest. The machine pmjc itself runs on is always available too (no SSH): under its hostname and `local`, and any configured machine named like the host or listed in `self_names` runs locally, so other devices can reach the pmjc host through pmjc. Local commands use `self_shell` (default cmd on Windows, matching Windows OpenSSH's default shell; sh elsewhere). `include_self: false` turns this off.
- `lmstudio`: option `hosts` (`{name: url}`). `lmstudio_ask` uses only an already-loaded model; loading and unloading need approval.

## Declaring limits
Set `limitations` (what the connector can't do, prerequisites such as where an id comes from) and `native_alternatives` (what a client with better abilities should use) as class attributes on a Python connector, or as top-level keys in a YAML file. The registry appends both to every tool's description (`Limits: ...`, `Better if your client can: ...`), so a model that knows nothing else sees them in the tool list. Also say in each tool's docstring how to get the ids it needs (for example "channel_id comes from discord_channels"). Add the task to `capabilities.CATALOG` and run `uv run scripts/gen_capabilities.py` to refresh [CAPABILITIES.md](CAPABILITIES.md).

## eBay keys (the `ebay` connector)

1. Join the eBay developer program (free): <https://developer.ebay.com/>, then open **Application Keys**.
2. Create an app (keyset). Use the **Production** keyset (the Sandbox one only sees test listings; set `connectors.ebay.sandbox: true` in settings to use it).
3. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**, then enter them yourself: `uv run pmjc auth set ebay` or the dashboard's Connectors tab. They go to the credential store, never to a file.
4. Optional settings under `connectors.ebay`: `marketplace` (default `EBAY_US`), `sandbox` (default false).

Gotchas:

- **`401 invalid_client` from `/identity/v1/oauth2/token`:** the Production keyset isn't active yet. eBay disables it until you handle Marketplace Account Deletion notifications (the **Notifications** link next to the App ID). pmjc only reads public listings with an app token and stores no eBay user data, so choose the opt-out ("not persisting eBay data"); activation can take a few minutes.
- **`uv run pmjc auth set ebay` fails with "os error 32" (file in use):** a running pmjc (service or MCP server) locks `pmjc.exe` and uv tries to re-sync. Use `uv run --no-sync pmjc auth set ebay`.
- **Loose keyword matches:** a search for one model also returns other models, accessories and multi-model titles. Set `category_id` (e.g. 27386 for graphics cards) and filter titles in the watch.

`ebay_search` takes a list of queries (max 10) so a whole watch fits in one call; after a run reads listings it cannot make another egress call.

## Web search and page fetch (the `web` connector)

`web_search` (up to 5 queries per call) and `web_fetch` (up to 5 URLs per call, readable text) give runs on local models the reach of a client's own web tools. Both return third-party text and send queries/URLs off the machine, so after one call a run is tainted and can't make another egress call: the model puts everything into one list. `web_fetch` also stays allowed after taint for URLs the owner declared in a job or watch (`read_urls`, `read_prefixes`, `read_sites`; every URL in the call must match; read_sites never matches a URL with a query string).

Pick a search backend under `connectors.web` in settings (`web_fetch` works without one):

- **SearXNG (self-hosted, free, private).** Run an instance, add `json` to `search.formats` in its `settings.yml` (it is off by default), then set `backend: searxng` and `searxng_url: http://host:8080`.
- **Brave Search API.** Create a key at <https://api.search.brave.com/>, set `backend: brave`, then enter the key yourself with `uv run pmjc auth set web` (or the dashboard). It goes to the credential store, never to a file.

Other options: `blocked_domains` (list; subdomains included), `allow_private` (default false: addresses that resolve to loopback, private, link-local or similar ranges are refused after DNS resolution, redirects included), `max_bytes` (download cap, default 2000000). Fetching is plain HTTP with no JavaScript or logins; use `browser_read` for those.

## Mounting existing MCP servers

Reuse the MCP servers other people wrote (files, GitHub, Notion, calendars...). Add them to `settings.yaml` (edit the file; the API can't):

```yaml
mcp_servers:
  - id: files                      # tools become files__<tool>
    transport: stdio               # stdio | http
    command: npx                   # argv, never a shell
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/share"]
    env: [SOME_TOKEN]              # optional: read from the credential store as mcp.files.SOME_TOKEN
    tools: [read_text_file, list_directory]   # optional allowlist; `exclude: [...]` removes tools
    overrides:                     # optional per-tool changes to the safe defaults
      list_directory: {scope: read, egress: false}
  - id: remote
    transport: http
    url: https://example.com/mcp
    bearer_credential: mcp.remote.bearer      # credential-store key (the default); set it with `pmjc auth secret`
```

Safe defaults: every tool is `act` (needs approval) unless the server marks it `readOnlyHint: true`, and every tool is `untrusted_output` and `egress` unless you override it. `uv run pmjc mcp list` shows each server's health, tools and effective flags. See DECISIONS.md.


### Example: a notes vault (Obsidian or any Markdown folder)

Obsidian keeps notes as plain Markdown files, so the official filesystem server is enough. Pin its version, and mark the read tools as non-egress (reading a local file sends nothing out), so a run can still search the web after reading a note:

```yaml
mcp_servers:
  - id: notes
    transport: stdio
    command: npx            # on Windows use the full path to npx.cmd
    args: ["-y", "@modelcontextprotocol/server-filesystem@2.2.0", "/path/to/vault"]
    overrides:
      read_text_file: {egress: false}
      read_multiple_files: {egress: false}
      list_directory: {egress: false}
      directory_tree: {egress: false}
      search_files: {egress: false}
      get_file_info: {egress: false}
```

Write, edit, move and copy stay `act` tools, so every change to a note needs your approval. Check the result with `pmjc mcp list`.
