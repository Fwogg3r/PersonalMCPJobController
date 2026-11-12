"""Home Assistant: device states, services, phone location (last fix only) and phone notifications."""
from __future__ import annotations

from .. import config
from ..connector import Connector, Field, Health, tool


class HomeAssistant(Connector):
    id = "home_assistant"
    group = "home"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['lights', 'light', 'lamp', 'plug', 'switch', 'thermostat', 'scene', 'home', 'turn on', 'turn off', 'location']  # request words that select this connector (tool_mode: select)
    limitations = 'Only what Home Assistant exposes; protected entities (locks, alarms, ...) are refused. Location is the last fix from the phone app.'
    native_alternatives = 'a Home Assistant connector in your client.'
    name = "Home Assistant"
    description = "States, services, your phone's location and push notifications (with approve buttons) through Home Assistant."
    docs_url = "https://developers.home-assistant.io/docs/api/rest/"
    credentials = [
        Field("url", "Home Assistant URL", secret=False, help="e.g. http://127.0.0.1:8123"),
        Field("token", "Long-lived access token", help="Profile -> Security -> Long-lived access tokens"),
        Field("notify_service", "Notify service", secret=False, required=False,
              help="e.g. mobile_app_my_iphone (for notifications and approvals)"),
    ]

    async def _api(self, method: str, path: str, json: dict | None = None):
        r = await self.http.request(method, self.secret("url").rstrip("/") + "/api/" + path.lstrip("/"),
                                    json=json, headers={"Authorization": f"Bearer {self.secret('token')}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def health(self) -> Health:
        if not self.configured():
            return Health(False, "needs url and token")
        try:
            await self._api("GET", "")
            return Health(True, "connected")
        except Exception as e:
            return Health(False, type(e).__name__)

    @tool(scope="read")
    async def ha_states(self, domain: str | None = None, search: str | None = None, limit: int = 100) -> list[dict]:
        """Entity states, optionally filtered by domain (light, switch, sensor, person, ...) or a name search."""
        out = []
        for s in await self._api("GET", "states"):
            eid = s["entity_id"]
            name = s.get("attributes", {}).get("friendly_name", "")
            if domain and not eid.startswith(domain + "."):
                continue
            if search and search.lower() not in (eid + " " + name).lower():
                continue
            out.append({"entity_id": eid, "name": name, "state": s["state"]})
        return out[:limit]

    @tool(scope="read")
    async def ha_location(self, entity_id: str | None = None) -> dict:
        """Last known location of a person or device tracker (latitude, longitude, zone). Defaults to the first person."""
        if not entity_id:
            people = [s for s in await self._api("GET", "states") if s["entity_id"].startswith("person.")]
            if not people:
                return {"error": "no person entities"}
            s = people[0]
        else:
            s = await self._api("GET", f"states/{entity_id}")
        a = s.get("attributes", {})
        return {"entity_id": s["entity_id"], "zone": s["state"], "latitude": a.get("latitude"),
                "longitude": a.get("longitude"), "accuracy_m": a.get("gps_accuracy"), "updated": s.get("last_updated")}

    @tool(scope="act")
    async def ha_call_service(self, domain: str, service: str, entity_id: str | None = None, data: dict | None = None) -> dict:
        """Call a Home Assistant service, e.g. domain=light service=turn_on entity_id=light.kitchen. Find entity ids with ha_states first."""
        import re
        blocked = set(self.option("blocked_entities", []))
        patterns = self.option("blocked_patterns", [])
        ids = [entity_id] if entity_id else []
        ids += list((data or {}).get("entity_id", []) if isinstance((data or {}).get("entity_id"), list) else
                    [(data or {}).get("entity_id")] if (data or {}).get("entity_id") else [])
        if not ids and domain not in ("notify", "persistent_notification"):
            raise PermissionError("give an explicit entity_id (calls without a target could switch protected devices)")
        for e in ids:
            if e in blocked or any(re.search(p, e) for p in patterns):
                raise PermissionError(f"{e} is protected (home_assistant blocked_entities / blocked_patterns)")
        body = dict(data or {})
        if entity_id:
            body["entity_id"] = entity_id
        return {"changed": await self._api("POST", f"services/{domain}/{service}", body)}

    async def notify(self, title: str, message: str, actions: list[dict] | None = None) -> None:
        svc = self.secret("notify_service")
        data: dict = {"title": title, "message": message}
        public = config.load().public_url.rstrip("/")
        if actions and public:
            data["data"] = {"actions": [{"action": "URI", "title": a["title"], "uri": public + a["path"],
                                         "authenticationRequired": True} for a in actions]}
        await self._api("POST", f"services/notify/{svc}", data)
