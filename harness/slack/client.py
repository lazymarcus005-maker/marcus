import httpx


class SlackClientError(Exception):
    pass


class SlackClient:
    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str = "https://slack.com/api",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")
        self._client = http_client or httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        self._owns_client = http_client is None

    async def post_message(self, *, channel: str, thread_ts: str, text: str) -> dict:
        if not self.bot_token:
            raise SlackClientError("slack bot token is not configured")

        response = await self._client.post(
            "/chat.postMessage",
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json={"channel": channel, "thread_ts": thread_ts, "text": text},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise SlackClientError(payload.get("error", "slack api error"))
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
