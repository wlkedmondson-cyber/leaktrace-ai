import requests


def wind_direction_to_cardinal(degrees):
    if degrees is None:
        return "unknown"

    directions = [
        "N", "NNE", "NE", "ENE",
        "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW",
        "W", "WNW", "NW", "NNW"
    ]

    idx = round(float(degrees) / 22.5) % 16
    return directions[idx]


def get_weather_summary(address):
    """
    Pulls location + current/near-term weather from Open-Meteo.

    Returns:
    {
        "lat": ...,
        "lon": ...,
        "rainfall": "...",
        "wind_speed": "...",
        "wind_direction": "...",
        "summary": "..."
    }
    """

    if not address:
        return None

    try:
        geo_response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": address,
                "count": 1,
                "language": "en",
                "format": "json"
            },
            timeout=10
        )

        geo_response.raise_for_status()
        geo = geo_response.json()

        if not geo.get("results"):
            return None

        result = geo["results"][0]
        lat = result.get("latitude")
        lon = result.get("longitude")

        if lat is None or lon is None:
            return None

        weather_response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": [
                    "temperature_2m",
                    "precipitation",
                    "rain",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "weather_code"
                ],
                "daily": [
                    "precipitation_sum",
                    "rain_sum",
                    "wind_speed_10m_max"
                ],
                "timezone": "auto",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch"
            },
            timeout=10
        )

        weather_response.raise_for_status()
        weather = weather_response.json()

        current = weather.get("current", {})
        daily = weather.get("daily", {})

        current_wind_speed = current.get("wind_speed_10m")
        current_wind_direction_degrees = current.get("wind_direction_10m")
        current_wind_direction = wind_direction_to_cardinal(
            current_wind_direction_degrees
        )

        rainfall_today = None
        precipitation_values = daily.get("precipitation_sum")

        if precipitation_values and len(precipitation_values) > 0:
            rainfall_today = precipitation_values[0]

        wind_summary = "unknown"
        if current_wind_speed is not None:
            wind_summary = f"{current_wind_speed} mph from {current_wind_direction}"

        rain_summary = "unknown"
        if rainfall_today is not None:
            rain_summary = f"{rainfall_today} in today"

        location_name = result.get("name", address)
        admin_area = result.get("admin1", "")
        country = result.get("country", "")

        location_display = ", ".join(
            part for part in [location_name, admin_area, country] if part
        )

        summary = (
            f"{location_display}: "
            f"rainfall {rain_summary}; "
            f"wind {wind_summary}"
        )

        return {
            "lat": lat,
            "lon": lon,
            "rainfall": rain_summary,
            "wind_speed": wind_summary,
            "wind_direction": current_wind_direction,
            "summary": summary,
            "raw": weather
        }

    except Exception as e:
        print("Weather error:", e)
        return None