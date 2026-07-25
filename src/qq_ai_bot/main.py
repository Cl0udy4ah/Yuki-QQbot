"""NoneBot2 application entrypoint and lifespan wiring."""

from __future__ import annotations

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.drivers.fastapi import Driver as FastAPIDriver

from qq_ai_bot.config import Settings
from qq_ai_bot.container import ApplicationContainer, get_container, set_container
from qq_ai_bot.health import HealthPayload, build_health_payload
from qq_ai_bot.logging import configure_logging


def bootstrap(settings: Settings | None = None) -> None:
    """Configure NoneBot, routes, adapters, plugins, and resource lifecycle."""

    app_settings = settings or Settings()
    nonebot.init(
        driver="~fastapi",
        host=app_settings.app_host,
        port=app_settings.app_port,
        log_level=app_settings.log_level,
        superusers=set(app_settings.superusers),
        onebot_access_token=app_settings.onebot_access_token or None,
    )
    configure_logging(app_settings.log_level)
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)

    @driver.on_startup
    async def startup() -> None:
        container = await ApplicationContainer.create(app_settings)
        set_container(container)
        await container.start()

    @driver.on_shutdown
    async def shutdown() -> None:
        await get_container().close()

    async def healthz() -> HealthPayload:
        return await build_health_payload(get_container())

    if not isinstance(driver, FastAPIDriver):
        raise RuntimeError("FastAPI driver is required")
    driver.server_app.add_api_route("/healthz", healthz, methods=["GET"])

    nonebot.load_plugin("qq_ai_bot.plugins.ai_chat")


def run() -> None:
    """Run the ASGI server until SIGINT or SIGTERM."""

    bootstrap()
    nonebot.run()


if __name__ == "__main__":
    run()
