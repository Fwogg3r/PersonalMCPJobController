"""Free utilities: weather (Open-Meteo), places (OpenStreetMap), GitHub."""
from __future__ import annotations

import re

from ..connector import Connector, Field, Health, tool

UA = {"User-Agent": "pmjc/0.1 (+https://github.com/Fwogg3r/PersonalMCPJobController)"}


async def geocode(http, place: str) -> dict:
    r = await http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1})
    r.raise_for_status()
    res = (r.json().get("results") or [None])[0]
    if not res:
        raise ValueError(f"couldn't find {place!r}")
    return {"lat": res["latitude"], "lon": res["longitude"], "name": f"{res['name']}, {res.get('admin1', '')} {res.get('country_code', '')}".strip()}


class Weather(Connector):
    id = "weather"
    aliases = ['weather', 'forecast', 'rain', 'temperature', 'umbrella', 'snow']  # request words that select this connector (tool_mode: select)
    limitations = 'Forecasts only (Open-Meteo); no radar, alerts or history.'
    native_alternatives = 'a weather or search connector in your client.'
    name = "Weather (Open-Meteo)"
    description = "Current weather and daily forecast anywhere. Free, no account."
    docs_url = "https://open-meteo.com/"

    async def health(self) -> Health:
        return Health(True, "no account needed")

    @tool(scope="read", egress=True, private_output=False)
    async def weather_forecast(self, place: str | None = None, lat: float | None = None, lon: float | None = None,
                               days: int = 3, units: str = "fahrenheit") -> dict:
        """Current conditions and a daily forecast for a place name or coordinates (use ha_location for 'near me')."""
        where = place or f"{lat},{lon}"
        if place and lat is None:
            g = await geocode(self.http, place)
            lat, lon, where = g["lat"], g["lon"], g["name"]
        if lat is None or lon is None:
            raise ValueError("give a place or lat/lon")
        r = await self.http.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "forecast_days": min(max(days, 1), 16), "timezone": "auto",
            "temperature_unit": "fahrenheit" if units.startswith("f") else "celsius",
            "wind_speed_unit": "mph" if units.startswith("f") else "kmh",
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        r.raise_for_status()
        d = r.json()
        return {"place": where, "current": d.get("current"), "daily": d.get("daily"),
                "weather_codes": "https://open-meteo.com/en/docs#weathervariables"}


class Places(Connector):
    id = "places"
    aliases = ['nearby', 'near me', 'restaurant', 'shop', 'geocode', 'address', 'place', 'map']  # request words that select this connector (tool_mode: select)
    limitations = 'OpenStreetMap data: no reviews, ratings, prices or live opening status.'
    native_alternatives = 'a maps or search connector in your client.'
    name = "Places (OpenStreetMap)"
    description = "Find restaurants, shops and other places near a location. Free, no account."
    docs_url = "https://wiki.openstreetmap.org/wiki/Overpass_API"

    async def health(self) -> Health:
        return Health(True, "no account needed")

    @tool(scope="read", egress=True, private_output=False)
    async def places_nearby(self, lat: float, lon: float, kind: str = "restaurant", radius_m: int = 2000,
                            cuisine: str | None = None, limit: int = 15) -> list[dict]:
        """Places of a kind near coordinates. kind: restaurant, fast_food, cafe, bar, pharmacy, fuel, supermarket, ...
        cuisine filters restaurants (pizza, mexican, ...)."""
        kind = re.sub(r"[^a-z_]", "", kind.lower())  # model-controlled: keep the Overpass query well-formed
        cuisine = re.sub(r"[^a-z_ ;-]", "", (cuisine or "").lower()) or None
        key = "shop" if kind in ("supermarket", "convenience", "bakery") else "amenity"
        flt = f'["{key}"="{kind}"]' + (f'["cuisine"~"{cuisine}",i]' if cuisine else "")
        radius = min(max(radius_m, 100), 20000)
        q = f"[out:json][timeout:25];nwr{flt}(around:{radius},{lat},{lon});out center {min(limit * 3, 150)};"
        r = await self.http.post("https://overpass-api.de/api/interpreter", data={"data": q}, headers=UA, timeout=40)
        r.raise_for_status()
        out = []
        for e in r.json().get("elements", []):
            t = e.get("tags", {})
            if not t.get("name"):
                continue
            c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
            out.append({"name": t["name"], "cuisine": t.get("cuisine"), "hours": t.get("opening_hours"),
                        "phone": t.get("phone"), "website": t.get("website"), "lat": c["lat"], "lon": c["lon"],
                        "address": " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")]))})
        return out[:limit]

    @tool(scope="read", egress=True, private_output=False)
    async def places_geocode(self, place: str) -> dict:
        """Coordinates for a place name or address."""
        return await geocode(self.http, place)


class GitHub(Connector):
    id = "github"
    aliases = ['github', 'issue', 'pull request', 'repo']  # request words that select this connector (tool_mode: select)
    limitations = "Notifications, issue/PR search and comments only; what is visible depends on the token's scopes. No code access, no merging."
    native_alternatives = 'a GitHub connector or the gh CLI in your client.'
    name = "GitHub"
    description = "Your GitHub notifications, issue/PR search and comments."
    docs_url = "https://github.com/settings/personal-access-tokens"
    credentials = [Field("token", "Personal access token")]

    async def _api(self, method, path, **kw):
        r = await self.http.request(method, "https://api.github.com" + path, headers={
            "Authorization": f"Bearer {self.secret('token')}", "Accept": "application/vnd.github+json"}, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    @tool(scope="read", untrusted_output=True)
    async def github_notifications(self, all: bool = False) -> list[dict]:
        """Your unread GitHub notifications."""
        return [{"repo": n["repository"]["full_name"], "type": n["subject"]["type"], "title": n["subject"]["title"],
                 "reason": n["reason"], "updated": n["updated_at"]}
                for n in await self._api("GET", "/notifications", params={"all": str(all).lower()})]

    @tool(scope="read", untrusted_output=True, egress=True)
    async def github_search_issues(self, query: str, limit: int = 10) -> list[dict]:
        """Search issues and pull requests (GitHub search syntax, e.g. 'repo:owner/name is:open label:bug')."""
        d = await self._api("GET", "/search/issues", params={"q": query, "per_page": min(limit, 50)})
        return [{"title": i["title"], "url": i["html_url"], "state": i["state"], "user": i["user"]["login"]} for i in d["items"]]

    @tool(scope="act")
    async def github_comment(self, repo: str, number: int, body: str) -> dict:
        """Comment on an issue or pull request (repo = owner/name; number from github_search_issues or github_notifications)."""
        d = await self._api("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        return {"url": d["html_url"]}
