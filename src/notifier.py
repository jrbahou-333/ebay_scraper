"""Telegram alerts. One message per listing, with the photo when available.

Deliberately minimal: price, linked title, and "Close by" when it's within
driving range. Everything else is one tap away on the listing itself.
"""

import html

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def is_close(distance_km, pickup_km) -> bool:
    """Within collection range (by car). Unknown distance or no pickup_km → False."""
    return distance_km is not None and pickup_km is not None and distance_km <= pickup_km


class Notifier:
    def __init__(self, token: str, chat_id: str, pickup_km=None):
        self._token = token
        self._chat_id = chat_id
        self._pickup_km = pickup_km
        self._session = requests.Session()

    def _call(self, method: str, payload: dict) -> bool:
        resp = self._session.post(
            API.format(token=self._token, method=method), data=payload, timeout=30
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        # Surface Telegram's reason (bad chat_id, blocked bot, caption too long, ...).
        print(f"  Telegram {method} failed ({resp.status_code}): {resp.text[:200]}")
        return False

    def send_text(self, text: str) -> bool:
        return self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
        )

    def _send(self, caption: str, image: str | None) -> bool:
        """sendPhoto when there's an image; fall back to a text message."""
        if image:
            ok = self._call(
                "sendPhoto",
                {
                    "chat_id": self._chat_id,
                    "photo": image,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
            )
            if ok:
                return True
            # Photo can fail (dead URL / caption length); fall back to text.
        return self.send_text(caption)

    def send_listing(self, row: dict) -> bool:
        """Alert a new (Buy It Now) listing. `row` is a dict from db.fetch_unnotified."""
        return self._send(_format(row, self._pickup_km), row.get("image_url"))

    def send_ending(self, row: dict, minutes_left: int) -> bool:
        """Alert an auction in its last hour; `row['price_minor']` is the live bid."""
        return self._send(_format_ending(row, minutes_left, self._pickup_km), row.get("image_url"))


def _format(row: dict, pickup_km=None) -> str:
    price = _price_str(row.get("price_minor"), row.get("currency") or "GBP")
    title = html.escape(row.get("title") or "(no title)")

    # Title doubles as the link to the listing (falls back to plain text if the
    # URL is ever missing). quote=True: eBay URLs contain & and land in an attr.
    if row.get("url"):
        title = f'<a href="{html.escape(row["url"], quote=True)}">{title}</a>'

    lines = [f"<b>{price} — {title}</b>"]
    if is_close(row.get("distance_km"), pickup_km):
        lines.append("📍 Close by")
    return "\n".join(lines)


def _format_ending(row: dict, minutes_left: int, pickup_km=None) -> str:
    return f"⏰ Auction ends in {minutes_left} min\n" + _format(row, pickup_km)


def _price_str(price_minor, currency: str) -> str:
    if price_minor is None:
        return "—"
    pounds = price_minor / 100
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{sym}{pounds:.0f}" if pounds == int(pounds) else f"{sym}{pounds:.2f}"
