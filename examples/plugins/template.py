"""Copy this file to <PMJC_HOME>/plugins/<name>.py and edit. See docs/CONNECTORS.md."""
from pmjc.connector import Connector, Event, Field, event_source, tool


class Example(Connector):
    id = "example"
    name = "Example service"
    description = "What this connects to, in one sentence."
    docs_url = "https://example.com/developers"
    credentials = [Field("api_key", "API key", help="Where to find it")]

    async def _api(self, method: str, path: str, **kw):
        r = await self.http.request(method, "https://api.example.com" + path,
                                    headers={"Authorization": f"Bearer {self.secret('api_key')}"}, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    @tool(scope="read", untrusted_output=True)
    async def example_list(self, limit: int = 10) -> list[dict]:
        """List recent items."""
        return (await self._api("GET", "/items", params={"limit": limit}))["items"]

    @tool(scope="act")
    async def example_create(self, title: str) -> dict:
        """Create an item (needs approval)."""
        return await self._api("POST", "/items", json={"title": title})

    @event_source(interval=300)
    async def poll(self):
        """Optional: new items become events that jobs can react to."""
        since = self.state_get("cursor", 0)
        items = (await self._api("GET", "/items", params={"since": since}))["items"]
        if items:
            self.state_set("cursor", max(i["id"] for i in items))
        return [Event(self.id, "item", title=i["title"], text=i.get("body", ""), key=f"item:{i['id']}") for i in items]
