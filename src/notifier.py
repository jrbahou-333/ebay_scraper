"""Telegram alerts. One message per new listing, with the photo when available."""

import html

import requests

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
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

    def send_listing(self, row: dict) -> bool:
        """Send one listing. Tries sendPhoto; falls back to a text message.

        `row` is a dict from db.fetch_unnotified plus a 'highlights' list.
        """
        caption = _format(row)
        image = row.get("image_url")
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


def _format(row: dict) -> str:
    highlights = row.get("highlights") or []
    icon = "🔧" if highlights else "🏷"

    price = _price_str(row.get("price_minor"), row.get("currency") or "GBP")
    title = html.escape(row.get("title") or "(no title)")

    where = []
    if row.get("location"):
        where.append(html.escape(str(row["location"])))
    if row.get("distance_km") is not None:
        where.append(f"{row['distance_km']:g} km")
    where_str = " · ".join(where)

    tags = []
    if highlights:
        tags.append("⭐ " + ", ".join(html.escape(h) for h in highlights))
    if row.get("condition"):
        tags.append(html.escape(str(row["condition"])))
    if row.get("search_query"):
        tags.append("search: " + html.escape(str(row["search_query"])))

    # Title doubles as the link to the listing (falls back to plain text if the
    # URL is ever missing). quote=True: eBay URLs contain & and land in an attr.
    if row.get("url"):
        title = f'<a href="{html.escape(row["url"], quote=True)}">{title}</a>'

    lines = [f"{icon} <b>{price} — {title}</b>" + (f" — {where_str}" if where_str else "")]
    if tags:
        lines.append(" · ".join(tags))
    return "\n".join(lines)


def _price_str(price_minor, currency: str) -> str:
    if price_minor is None:
        return "—"
    pounds = price_minor / 100
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{sym}{pounds:.0f}" if pounds == int(pounds) else f"{sym}{pounds:.2f}"
