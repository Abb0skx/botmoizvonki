import asyncio


class ForwardingScheduler:
    def __init__(self, service):
        self.service = service
        self._task = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name="forwarding-control-scheduler",
        )

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.service.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("FORWARDING SCHEDULER ERROR:", repr(exc))
            await asyncio.sleep(self.service.settings.poll_seconds)
