"""Flight and accommodation search providers.

**Why this is a mock, stated plainly.**

The brief suggested Amadeus's Self-Service API. Amadeus decommissioned that
portal on 17 July 2026 and disabled existing keys, so it is not an option.
The remaining free routes are Duffel's test sandbox - which returns invented
flights on a fictional airline with unrealistic prices - or a mock. The
assignment explicitly lists "mock flight/hotel API" as an acceptable tool, so
this ships a mock, and every result it produces is labelled
``"source": "mock"`` in the API response and in the UI. Nothing here is
presented as live availability.

**What makes it a defensible mock rather than random numbers.**

Fabricated data that ignores reality makes an agent look broken: a £40 fare
from London to Sydney, or a two-hour flight to the other side of the world,
undermines every genuine part of the system. So the generator is grounded in
things that are actually true:

* Distance is real - great-circle between real airport coordinates.
* Duration follows from distance at typical cruise speed, plus taxi and, on
  long routes, a connection.
* Price follows a published-looking curve: a fixed cost plus a per-kilometre
  rate that *decreases* with distance (long-haul is cheaper per km), then
  adjusted for cabin, how far ahead the booking is, and seasonality.
* Output is deterministic. The same query returns the same flights, seeded
  from the route and date, so a demo is reproducible and tests can assert on
  exact values.

`FlightProvider` is a Protocol with two implementations, selected by the
`FLIGHT_PROVIDER` setting. Adding a real provider means writing one class, not
editing the agent.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
# A curated set of major destinations: IATA code, coordinates, and a cost tier
# used for accommodation pricing. Cost tiers are ordinal (1 = inexpensive,
# 5 = very expensive) and reflect broad, well-known differences in city price
# levels rather than any specific data source.

_CITY_DATA: Final[dict[str, tuple[str, float, float, int]]] = {
    # city key            IATA    lat       lon      cost tier
    "tokyo": ("HND", 35.5494, 139.7798, 4),
    "osaka": ("KIX", 34.4342, 135.2440, 3),
    "kyoto": ("KIX", 34.9859, 135.7585, 3),
    "seoul": ("ICN", 37.4602, 126.4407, 3),
    "beijing": ("PEK", 40.0799, 116.6031, 3),
    "shanghai": ("PVG", 31.1443, 121.8083, 3),
    "hong kong": ("HKG", 22.3080, 113.9185, 4),
    "taipei": ("TPE", 25.0777, 121.2328, 3),
    "singapore": ("SIN", 1.3644, 103.9915, 4),
    "bangkok": ("BKK", 13.6900, 100.7501, 2),
    "kuala lumpur": ("KUL", 2.7456, 101.7099, 2),
    "jakarta": ("CGK", -6.1256, 106.6558, 2),
    "manila": ("MNL", 14.5086, 121.0194, 2),
    "hanoi": ("HAN", 21.2212, 105.8072, 1),
    "ho chi minh city": ("SGN", 10.8188, 106.6519, 1),
    "delhi": ("DEL", 28.5562, 77.1000, 2),
    "mumbai": ("BOM", 19.0896, 72.8656, 2),
    "bengaluru": ("BLR", 13.1986, 77.7066, 2),
    "chennai": ("MAA", 12.9941, 80.1709, 2),
    "hyderabad": ("HYD", 17.2403, 78.4294, 2),
    "kolkata": ("CCU", 22.6547, 88.4467, 2),
    "goa": ("GOI", 15.3808, 73.8314, 2),
    "dubai": ("DXB", 25.2532, 55.3657, 4),
    "doha": ("DOH", 25.2609, 51.6138, 4),
    "istanbul": ("IST", 41.2753, 28.7519, 2),
    "london": ("LHR", 51.4700, -0.4543, 5),
    "paris": ("CDG", 49.0097, 2.5479, 5),
    "amsterdam": ("AMS", 52.3105, 4.7683, 4),
    "berlin": ("BER", 52.3667, 13.5033, 3),
    "munich": ("MUC", 48.3537, 11.7750, 4),
    "zurich": ("ZRH", 47.4647, 8.5492, 5),
    "rome": ("FCO", 41.8003, 12.2389, 3),
    "milan": ("MXP", 45.6306, 8.7281, 4),
    "venice": ("VCE", 45.5053, 12.3519, 4),
    "barcelona": ("BCN", 41.2974, 2.0833, 3),
    "madrid": ("MAD", 40.4839, -3.5680, 3),
    "lisbon": ("LIS", 38.7756, -9.1354, 3),
    "vienna": ("VIE", 48.1103, 16.5697, 4),
    "prague": ("PRG", 50.1008, 14.2600, 2),
    "budapest": ("BUD", 47.4369, 19.2556, 2),
    "athens": ("ATH", 37.9364, 23.9445, 2),
    "copenhagen": ("CPH", 55.6180, 12.6508, 5),
    "stockholm": ("ARN", 59.6519, 17.9186, 4),
    "oslo": ("OSL", 60.1976, 11.1004, 5),
    "dublin": ("DUB", 53.4213, -6.2701, 4),
    "reykjavik": ("KEF", 63.9850, -22.6056, 5),
    "new york": ("JFK", 40.6413, -73.7781, 5),
    "boston": ("BOS", 42.3656, -71.0096, 4),
    "washington": ("IAD", 38.9531, -77.4565, 4),
    "chicago": ("ORD", 41.9742, -87.9073, 3),
    "los angeles": ("LAX", 33.9416, -118.4085, 4),
    "san francisco": ("SFO", 37.6213, -122.3790, 5),
    "seattle": ("SEA", 47.4502, -122.3088, 4),
    "miami": ("MIA", 25.7959, -80.2870, 4),
    "toronto": ("YYZ", 43.6777, -79.6248, 3),
    "vancouver": ("YVR", 49.1967, -123.1815, 4),
    "mexico city": ("MEX", 19.4361, -99.0719, 2),
    "sao paulo": ("GRU", -23.4356, -46.4731, 2),
    "rio de janeiro": ("GIG", -22.8100, -43.2506, 2),
    "buenos aires": ("EZE", -34.8222, -58.5358, 2),
    "lima": ("LIM", -12.0219, -77.1143, 2),
    "cairo": ("CAI", 30.1219, 31.4056, 1),
    "cape town": ("CPT", -33.9715, 18.6021, 2),
    "nairobi": ("NBO", -1.3192, 36.9278, 2),
    "marrakesh": ("RAK", 31.6069, -8.0363, 2),
    "sydney": ("SYD", -33.9399, 151.1753, 4),
    "melbourne": ("MEL", -37.6690, 144.8410, 4),
    "auckland": ("AKL", -37.0082, 174.7850, 4),
}

# Plausible carriers by broad region of the departure airport, so a flight out
# of Tokyo is not operated by a Latin American airline.
_CARRIERS: Final[dict[str, list[tuple[str, str]]]] = {
    "asia": [
        ("NH", "All Nippon Airways"),
        ("SQ", "Singapore Airlines"),
        ("CX", "Cathay Pacific"),
        ("JL", "Japan Airlines"),
        ("TG", "Thai Airways"),
    ],
    "india": [("AI", "Air India"), ("6E", "IndiGo"), ("UK", "Vistara")],
    "europe": [
        ("BA", "British Airways"),
        ("LH", "Lufthansa"),
        ("AF", "Air France"),
        ("KL", "KLM"),
        ("IB", "Iberia"),
    ],
    "americas": [
        ("AA", "American Airlines"),
        ("DL", "Delta Air Lines"),
        ("UA", "United Airlines"),
        ("AC", "Air Canada"),
    ],
    "oceania": [("QF", "Qantas"), ("NZ", "Air New Zealand")],
    "middle_east": [("EK", "Emirates"), ("QR", "Qatar Airways"), ("TK", "Turkish Airlines")],
    "africa": [("ET", "Ethiopian Airlines"), ("MS", "EgyptAir"), ("SA", "South African Airways")],
}

_CABIN_MULTIPLIER: Final[dict[str, float]] = {
    "economy": 1.0,
    "premium_economy": 1.6,
    "business": 3.2,
    "first": 5.5,
}

EARTH_RADIUS_KM: Final[float] = 6371.0
CRUISE_SPEED_KMH: Final[float] = 830.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres.

    Args:
        lat1: Latitude of the first point in degrees.
        lon1: Longitude of the first point in degrees.
        lat2: Latitude of the second point in degrees.
        lon2: Longitude of the second point in degrees.

    Returns:
        Distance in kilometres.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _region_for(latitude: float, longitude: float) -> str:
    """Classify coordinates into a broad region for carrier selection.

    Deliberately coarse - it only needs to prevent obviously wrong pairings.

    Args:
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.

    Returns:
        A region key present in `_CARRIERS`.
    """
    if -30 <= longitude <= 60 and latitude < 35:
        return "africa"
    if 35 <= longitude <= 65:
        return "middle_east"
    if 65 <= longitude <= 92 and 5 <= latitude <= 37:
        return "india"
    if longitude > 92 and latitude > -10:
        return "asia"
    if longitude > 110 and latitude <= -10:
        return "oceania"
    if -25 <= longitude <= 45:
        return "europe"
    return "americas"


def _seed(*parts: str) -> int:
    """Derive a stable integer seed from string components.

    Uses SHA-256 rather than `hash()` because Python randomises string hashing
    per process, which would make "deterministic" results change on every
    restart - exactly the kind of thing that breaks a demo.

    Args:
        *parts: Components identifying the query.

    Returns:
        A deterministic non-negative integer.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def resolve_city(name: str) -> tuple[str, str, float, float, int] | None:
    """Look up a city in the reference dataset.

    Args:
        name: City name, case-insensitive. A `"City, Country"` string is
            reduced to its first component.

    Returns:
        A tuple of (canonical name, IATA code, latitude, longitude, cost tier),
        or None if the city is not in the dataset.
    """
    key = name.split(",")[0].strip().lower()
    entry = _CITY_DATA.get(key)
    if entry is None:
        # Try a forgiving prefix match so "Tokyo Japan" or "New York City"
        # still resolve.
        for city, data in _CITY_DATA.items():
            if key.startswith(city) or city.startswith(key):
                entry = data
                key = city
                break
    if entry is None:
        return None
    iata, latitude, longitude, tier = entry
    return key.title(), iata, latitude, longitude, tier


def _seasonality(departure: date, latitude: float) -> float:
    """Price multiplier reflecting seasonal demand.

    Peaks in the local summer and around late December. Hemisphere-aware, so
    January is peak season for Sydney and low season for Stockholm.

    Args:
        departure: Departure date.
        latitude: Destination latitude, used to pick the hemisphere.

    Returns:
        A multiplier roughly in the range 0.85 to 1.30.
    """
    month = departure.month
    # Shift the northern-hemisphere curve by six months below the equator.
    effective_month = month if latitude >= 0 else ((month + 5) % 12) + 1

    # Summer peak around July/August.
    summer = 1.0 + 0.18 * math.cos((effective_month - 7.5) * math.pi / 6)
    # Holiday bump around the new year, independent of hemisphere.
    in_holiday_window = (month == 12 and departure.day >= 18) or (month == 1 and departure.day <= 5)
    holiday = 1.12 if in_holiday_window else 1.0
    return round(summer * holiday, 3)


def _advance_purchase_factor(departure: date, today: date) -> float:
    """Price multiplier reflecting how far ahead the booking is.

    Cheapest in the one-to-three-month window; sharply more expensive inside
    two weeks. Mirrors the shape of real airline revenue management without
    claiming to reproduce any particular carrier's pricing.

    Args:
        departure: Departure date.
        today: Reference date for "now".

    Returns:
        A multiplier roughly in the range 1.0 to 1.9.
    """
    days = (departure - today).days
    if days < 0:
        return 1.0
    if days <= 3:
        return 1.90
    if days <= 7:
        return 1.60
    if days <= 14:
        return 1.35
    if days <= 30:
        return 1.12
    if days <= 90:
        return 1.00
    return 1.06  # Very early booking is not usually the cheapest either.


@runtime_checkable
class FlightProvider(Protocol):
    """Interface every flight/accommodation backend must satisfy.

    Defined as a Protocol rather than an abstract base class so an
    implementation does not have to import from this module to satisfy it -
    useful for test doubles.
    """

    name: str
    is_live: bool

    async def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None,
        cabin: str,
        adults: int,
    ) -> dict[str, Any]:
        """Search for flight offers."""
        ...

    async def search_accommodation(
        self,
        city: str,
        check_in: str,
        check_out: str,
        guests: int,
        max_price_per_night: float | None,
    ) -> dict[str, Any]:
        """Search for places to stay."""
        ...


class MockFlightProvider:
    """Deterministic, physically-plausible flight and hotel generator.

    See the module docstring for why this exists and what makes its output
    defensible. Every result carries `"source": "mock"`.
    """

    name = "mock"
    is_live = False

    async def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None = None,
        cabin: str = "economy",
        adults: int = 1,
    ) -> dict[str, Any]:
        """Generate flight offers for a route.

        Args:
            origin: Departure city name.
            destination: Arrival city name.
            departure_date: Outbound date as `YYYY-MM-DD`.
            return_date: Optional inbound date as `YYYY-MM-DD`.
            cabin: One of economy, premium_economy, business, first.
            adults: Number of adult passengers.

        Returns:
            A dict with the resolved route and a list of offers.

        Raises:
            ConfigurationError: Never; declared for interface symmetry.
        """
        from_city = resolve_city(origin)
        to_city = resolve_city(destination)

        if from_city is None or to_city is None:
            unknown = origin if from_city is None else destination
            return {
                "source": "mock",
                "error": "unknown_city",
                "message": (
                    f"{unknown!r} is not in the mock provider's city dataset. "
                    f"Supported cities include: {', '.join(sorted(_CITY_DATA)[:12])} "
                    "and about 55 others."
                ),
                "offers": [],
            }

        from_name, from_iata, from_lat, from_lon, _ = from_city
        to_name, to_iata, to_lat, to_lon, _ = to_city

        if from_iata == to_iata:
            return {
                "source": "mock",
                "error": "same_airport",
                "message": f"{from_name} and {to_name} share the same airport ({from_iata}).",
                "offers": [],
            }

        departure = datetime.strptime(departure_date, "%Y-%m-%d").date()
        distance_km = _haversine_km(from_lat, from_lon, to_lat, to_lon)

        # Per-km rate tapers with distance: short hops are dominated by fixed
        # costs, long-haul benefits from scale.
        rate_per_km = 0.22 if distance_km < 1500 else 0.14 if distance_km < 5000 else 0.085
        base_fare = 45.0 + distance_km * rate_per_km

        multiplier = (
            _CABIN_MULTIPLIER.get(cabin, 1.0)
            * _seasonality(departure, to_lat)
            * _advance_purchase_factor(departure, date.today())
        )

        carriers = _CARRIERS[_region_for(from_lat, from_lon)]
        seed = _seed(from_iata, to_iata, departure_date, cabin)

        # Non-stop is only plausible under roughly 12,000 km.
        offer_shapes = (
            [
                ("non-stop", 0, 1.00),
                ("non-stop", 0, 1.18),
                ("1 stop", 1, 0.82),
            ]
            if distance_km <= 12000
            else [
                ("1 stop", 1, 1.00),
                ("1 stop", 1, 1.15),
                ("2 stops", 2, 0.88),
            ]
        )

        offers: list[dict[str, Any]] = []
        for index, (label, stops, shape_factor) in enumerate(offer_shapes):
            carrier_code, carrier_name = carriers[(seed + index) % len(carriers)]

            flight_hours = distance_km / CRUISE_SPEED_KMH
            # Taxi, climb and descent, plus roughly 1h45 per connection.
            total_hours = flight_hours + 0.6 + stops * 1.75
            # Departure times spread across the day, stable for a given query.
            depart_hour = (6 + (seed // (index + 1)) % 15) % 24

            price = base_fare * multiplier * shape_factor
            # +/-6% deterministic jitter so the three offers are not exact
            # multiples of one another.
            jitter = 1.0 + (((seed >> (index * 4)) % 13) - 6) / 100.0
            price = round(price * jitter * adults, 2)

            depart_at = datetime.combine(departure, datetime.min.time()) + timedelta(
                hours=depart_hour
            )
            arrive_at = depart_at + timedelta(hours=total_hours)

            offers.append(
                {
                    "offer_id": f"MOCK-{from_iata}{to_iata}-{seed % 10000}-{index}",
                    "carrier": carrier_name,
                    "flight_number": f"{carrier_code}{100 + (seed + index * 37) % 899}",
                    "origin": from_iata,
                    "destination": to_iata,
                    "departure_at": depart_at.strftime("%Y-%m-%d %H:%M"),
                    "arrival_at": arrive_at.strftime("%Y-%m-%d %H:%M"),
                    "duration_hours": round(total_hours, 1),
                    "stops": stops,
                    "stop_label": label,
                    "cabin": cabin,
                    "price_total_usd": price,
                    "price_per_adult_usd": round(price / max(adults, 1), 2),
                }
            )

        offers.sort(key=lambda offer: offer["price_total_usd"])

        result: dict[str, Any] = {
            "source": "mock",
            "route": {
                "from": f"{from_name} ({from_iata})",
                "to": f"{to_name} ({to_iata})",
                "distance_km": round(distance_km),
            },
            "departure_date": departure_date,
            "cabin": cabin,
            "adults": adults,
            "currency": "USD",
            "offers": offers,
        }

        if return_date:
            inbound = await self.search_flights(
                destination, origin, return_date, None, cabin, adults
            )
            result["return_date"] = return_date
            result["return_offers"] = inbound.get("offers", [])

        return result

    async def search_accommodation(
        self,
        city: str,
        check_in: str,
        check_out: str,
        guests: int = 2,
        max_price_per_night: float | None = None,
    ) -> dict[str, Any]:
        """Generate accommodation options for a city and date range.

        Args:
            city: City name.
            check_in: Arrival date as `YYYY-MM-DD`.
            check_out: Departure date as `YYYY-MM-DD`.
            guests: Number of guests.
            max_price_per_night: Optional budget ceiling in USD.

        Returns:
            A dict with the resolved city and a list of properties.
        """
        resolved = resolve_city(city)
        if resolved is None:
            return {
                "source": "mock",
                "error": "unknown_city",
                "message": f"{city!r} is not in the mock provider's city dataset.",
                "properties": [],
            }

        city_name, _, latitude, _, cost_tier = resolved
        arrive = datetime.strptime(check_in, "%Y-%m-%d").date()
        depart = datetime.strptime(check_out, "%Y-%m-%d").date()
        nights = max((depart - arrive).days, 1)

        # Nightly rate anchored on the city's cost tier, then seasonally
        # adjusted the same way flights are.
        tier_base = {1: 32.0, 2: 55.0, 3: 95.0, 4: 145.0, 5: 210.0}[cost_tier]
        seasonal = _seasonality(arrive, latitude)
        seed = _seed(city_name, check_in, check_out, str(guests))

        tiers = [
            ("Hostel / guesthouse", 0.40, 2),
            ("3-star hotel", 0.85, 3),
            ("4-star hotel", 1.35, 4),
            ("Boutique / 5-star", 2.40, 5),
        ]

        properties: list[dict[str, Any]] = []
        for index, (label, factor, stars) in enumerate(tiers):
            jitter = 1.0 + (((seed >> (index * 3)) % 15) - 7) / 100.0
            nightly = round(tier_base * factor * seasonal * jitter, 2)
            # Extra guests raise the rate but not proportionally: a double
            # room costs more than a single, not twice as much.
            nightly = round(nightly * (1.0 + 0.25 * max(guests - 2, 0)), 2)

            if max_price_per_night is not None and nightly > max_price_per_night:
                continue

            properties.append(
                {
                    "property_id": f"MOCK-HTL-{seed % 10000}-{index}",
                    "name": f"{city_name} {label}",
                    "category": label,
                    "stars": stars,
                    "guest_rating": round(6.8 + (seed >> index) % 28 / 10.0, 1),
                    "price_per_night_usd": nightly,
                    "total_price_usd": round(nightly * nights, 2),
                    "nights": nights,
                    "free_cancellation": bool((seed >> index) % 2),
                }
            )

        return {
            "source": "mock",
            "city": city_name,
            "check_in": check_in,
            "check_out": check_out,
            "nights": nights,
            "guests": guests,
            "currency": "USD",
            "budget_filter_usd": max_price_per_night,
            "properties": properties,
        }


class DuffelFlightProvider:
    """Live flight search through Duffel's API.

    Selected by setting ``FLIGHT_PROVIDER=duffel`` and supplying
    ``DUFFEL_API_TOKEN``. In Duffel's test mode the data is still synthetic -
    fictional "Duffel Airways" flights - so `is_live` reflects whether a
    production token is in use rather than merely whether the API was reached.

    This exists to demonstrate that the provider boundary is real: swapping
    backends requires no change to the tool, the agent, or the API.
    """

    name = "duffel"

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise the provider.

        Args:
            settings: Settings override, for tests.

        Raises:
            ConfigurationError: If no Duffel token is configured.
        """
        self.settings = settings or get_settings()
        token = self.settings.duffel_api_token.get_secret_value()
        if not token:
            raise ConfigurationError(
                "FLIGHT_PROVIDER is set to 'duffel' but DUFFEL_API_TOKEN is empty. "
                "Either supply a token from https://app.duffel.com or set "
                "FLIGHT_PROVIDER=mock."
            )
        self._token = token
        self.is_live = not token.startswith("duffel_test")

    async def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None = None,
        cabin: str = "economy",
        adults: int = 1,
    ) -> dict[str, Any]:
        """Search Duffel for flight offers.

        Args:
            origin: Departure city name.
            destination: Arrival city name.
            departure_date: Outbound date as `YYYY-MM-DD`.
            return_date: Optional inbound date.
            cabin: Requested cabin class.
            adults: Number of adult passengers.

        Returns:
            A dict in the same shape `MockFlightProvider` returns.
        """
        from app.services.http import request_json

        from_city = resolve_city(origin)
        to_city = resolve_city(destination)
        if from_city is None or to_city is None:
            return {
                "source": "duffel",
                "error": "unknown_city",
                "message": "Origin or destination could not be resolved to an airport.",
                "offers": [],
            }

        slices = [
            {
                "origin": from_city[1],
                "destination": to_city[1],
                "departure_date": departure_date,
            }
        ]
        if return_date:
            slices.append(
                {
                    "origin": to_city[1],
                    "destination": from_city[1],
                    "departure_date": return_date,
                }
            )

        payload = await request_json(
            "POST",
            "https://api.duffel.com/air/offer_requests",
            service="duffel",
            json_body={
                "data": {
                    "slices": slices,
                    "passengers": [{"type": "adult"} for _ in range(adults)],
                    "cabin_class": cabin,
                }
            },
            headers={
                "Authorization": f"Bearer {self._token}",
                "Duffel-Version": "v2",
                "Accept": "application/json",
            },
        )

        offers = []
        for offer in (payload.get("data", {}).get("offers") or [])[:5]:
            owner = offer.get("owner") or {}
            offers.append(
                {
                    "offer_id": offer.get("id"),
                    "carrier": owner.get("name"),
                    "price_total_usd": float(offer.get("total_amount") or 0.0),
                    "currency": offer.get("total_currency"),
                    "cabin": cabin,
                }
            )

        return {
            "source": "duffel",
            "live_data": self.is_live,
            "route": {"from": from_city[1], "to": to_city[1]},
            "departure_date": departure_date,
            "offers": offers,
        }

    async def search_accommodation(
        self,
        city: str,
        check_in: str,
        check_out: str,
        guests: int = 2,
        max_price_per_night: float | None = None,
    ) -> dict[str, Any]:
        """Delegate accommodation search to the mock provider.

        Duffel is a flights API and offers no accommodation product, so
        falling back keeps the tool's contract intact rather than returning an
        error the agent cannot act on.
        """
        logger.info("provider.accommodation_fallback", provider="duffel", fallback="mock")
        return await MockFlightProvider().search_accommodation(
            city, check_in, check_out, guests, max_price_per_night
        )


def get_flight_provider(settings: Settings | None = None) -> FlightProvider:
    """Return the configured flight provider.

    Args:
        settings: Settings override, for tests.

    Returns:
        The provider named by `FLIGHT_PROVIDER`. Falls back to the mock if a
        live provider is selected but misconfigured, because a degraded
        provider is better than a crashed application.
    """
    cfg = settings or get_settings()

    if cfg.flight_provider == "duffel":
        try:
            return DuffelFlightProvider(cfg)
        except ConfigurationError as exc:
            logger.warning("provider.duffel_unavailable", error=exc.message, fallback="mock")
            return MockFlightProvider()

    return MockFlightProvider()
